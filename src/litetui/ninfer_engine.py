"""Owning `ninfer-serve` from LiteTUI — the launcher the backend calls.

🔴 THE USER, 2026-09-17 (liteask a-35456da0): *"LiteTUI may start it"* — for 5090 end
users running LiteTUI WITHOUT LiteSuite. Until this module the backend could
only ATTACH (`ninfer_backend.py` docstring), which made LiteTUI-alone dead
without LiteSuite. Attaching is still the default; starting is `/engine start`.

REUSE, DO NOT REBUILD (the user, same day: *"agents ... rebuild every system for
every backend over and again instead of making modular resuable pieces"*):
  * the argv is LiteSuite's own (`apps/desktop/.../services/llm/ninfer-args.ts`
    buildNInferArgs) reproduced flag for flag, so an engine started here is the
    engine LiteSuite would have started — same context, same speculation, same
    fp8 KV, same `--preserve-thinking`;
  * the registry is LiteSuite's own `extraEndpoints[kind=ninfer]` in
    `~/.litesuite/llm/config.json` — write it on ready, remove it on stop —
    so LiteSuite's Local Backend page sees an engine LiteTUI started, and
    LiteTUI's discovery sees one LiteSuite started. ONE registry, two writers.
  * the artifact directory is read the way LiteSuite reads it (v3 container
    header: magic `NINFER\\0\\x03`, json_bytes u64 LE at 8, JSON at 32 —
    `ninfer-artifact.ts:326-330`), so `--spec` is gated on what the FILE holds.

🔴 VRAM: never a second model. `start()` refuses when ANY ninfer is registered
or answering, and the backend wraps it in LiteTUI's own `vram_guard` (T690: a
second LiteTUI instance). The process we start is the only one we ever stop.

# ponytail: VRAM headroom is a fixed number (weights 19.7 + runtime 1.6 + KV ~8
# GiB measured 2026-09-17 on the 27B nvfp4 at 32k fp8). Read the artifact's
# manifest for a size when a second artifact exists.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import engine_words, jobkill, ttyguard
from .llm_backend import BackendError, _arg_value
from .shared_state import Lease  # OwnershipError is an OSError subclass — caught by the writers below


def _registry_lock() -> Lease:
    """The cross-process lock EVERY writer of LiteSuite's config holds around its
    read-modify-write, so concurrent writers (LiteTUI instances/threads) never lose each
    other's entries. Non-blocking: OwnershipError (an OSError) on contention makes the
    write fail closed and the caller retains, rather than corrupting the shared file.
    (Interop is with LiteTUI's own writers; LiteSuite is a separate process and honours
    this lock only if it adopts the same path — a documented limitation.)"""
    return Lease(_config_path().with_name("config.json.lock"))


class EngineStartFailed(BackendError):
    """ninfer-serve failed to become ready. ``retained_owned`` carries the OwnedEngine for
    a resident-but-failed process whose cleanup could NOT be confirmed, so the caller can
    publish it and retry the stop; it is None when the cleanup was confirmed (nothing left
    to own). A BackendError subclass, so existing ``except BackendError`` paths still catch."""

    def __init__(self, message: str, *, retained_owned=None, reason: str | None = None) -> None:
        super().__init__(message)
        self.retained_owned = retained_owned
        #: Set (engine_words.REASON_*) when the same settings will fail the same way again, so
        #: nothing starts them unchanged a second time (T1338).
        self.reason = reason

NINFER_V3_MAGIC = b"NINFER\x00\x03"
NINFER_V3_HEADER_BYTES = 32
NINFER_MAX_DIRECTORY_BYTES = 64 * 1024 * 1024

#: LiteSuite's defaults (ninfer-args.ts). A user's chosen context wins.
NINFER_DEFAULT_MAX_CONTEXT = 32768
NINFER_DEFAULT_SPEC = "mtp"
NINFER_DEFAULT_DRAFT_TOKENS = 3
# dflash reaches 15: a DSpine draft (Tencent, 16-token blocks) rides the dflash component and was
# measured at 15 draft tokens (ninfer-all fork 66931cd0, 2026-11-10). 15 is also the engine's
# widest verified window.
NINFER_DRAFT_TOKEN_RANGE = {"mtp": (1, 3), "dflash": (1, 15), "dflash2": (1, 7)}
NINFER_DEFAULT_KV_DTYPE = "fp8"
#: `--kv-dtype` choices (serving.md:775). fp8 stays the default: the envelope above
#: (NINFER_MIN_FREE_MIB) was measured at fp8.
NINFER_KV_DTYPES = ("bf16", "int8", "fp8", "nvfp4", "k8v4")
#: T1383: the KV types a model family takes, keyed by the artifact header's `model_type`
#: (artifact_model_type). A family listed here gets `--kv-dtype` only when the user's choice is
#: in its tuple; otherwise NO `--kv-dtype` is sent and the engine's own default for that family
#: applies. Inkling 2 takes no fp8 (its default is mxfp8, `ninfer-serve --help` 2026-11-21), so
#: the stock fp8 setting used to make every Inkling start fail unless the model's defaults file forced bf16.
#: A family not listed keeps NINFER_KV_DTYPES and the fp8 default, exactly as before.
NINFER_FAMILY_KV_DTYPES = {
    "inkling2": ("bf16", "mxfp8", "rk8v4", "rk4v4"),
}
#: T1383: the most `--max-concurrency` a start WITHOUT model defaults sends for a family, and why, whatever
#: ninfer_max_concurrency says (model defaults that want parallel lanes still get them). 1 = the flag is
#: omitted, the engine's own default of one sequence. Measured 2026-11-21, Inkling 2 Mini IQ4_XS, same
#: build: no flag 235.6 tok/s, KV 3,440 MiB, 23,151 MiB VRAM, drafter on; --max-concurrency 4
#: 168.4 tok/s, KV 11,313 MiB, 30,398 MiB VRAM, no drafter ("serving one request at a time", its --help).
#: A family not listed is sent the user's value, exactly as before.
NINFER_FAMILY_MAX_CONCURRENCY = {
    "inkling2": (1, "it keeps its drafter (on only when serving one request at a time) and a single sequence's KV"),
}

#: LOOPKV-466 (Ryan 2026-11-24: "make it the default going forward. No other fallbacks. It should be on."):
#: the families whose start carries `--loop-kv MODE` by default. NInfer refuses the flag for every other
#: family (it is Inkling 2's looped core: the earlier passes read the last pass's KV), so only they are
#: listed. Measured 2026-11-24, Inkling 2 Mini Uncensored Q4_K_M, 150K: last-full loads 1,124 MiB lower,
#: 420K fits with 4.5 GiB free (per-pass 1.0 GiB); the long-prompt retrieval answer was the same file.
#: It is lossy (greedy answers differ from per-pass in wording), which is why the engine ships it off.
NINFER_FAMILY_LOOP_KV = {
    "inkling2": "last-full",
}

#: The engine's own words (ninfer-args.ts:184/195). A failure line ends the wait
#: NOW rather than at the timeout.
NINFER_READY_MARKER = "listening on http://"
NINFER_FAILURE_MARKERS = ("cannot bind", "server listen failed", "server failed during startup")
#: How long the engine may print NOTHING NEW before a start is called stalled (T1311). Not a
#: wall on the whole start: Edge0 reads 58 GB and warms CUDA graphs after that, so a fixed
#: 180 s ended starts that were still making progress. Every new byte in the log restarts it.
NINFER_START_TIMEOUT_S = 180
#: What the start wait reads out of the log as progress (T1311, Ryan: "Send auto-starts",
#: with Sentinel's constraint that the weight load shows progress). The engine prints, in
#: order: `loading weights 82.7% | 45.0 GiB/54.4 GiB | ... | ETA 2.0s` (repeated),
#: `weights ready`, `CUDA graphs ready`, `engine ready`, then the ready marker.
_PROGRESS_RE = re.compile(r"loading weights\b[^\r\n]*|weights ready|CUDA graphs ready|engine ready", re.I)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
#: A weight-load line is reported once per this many percent, so a 58 GB read is ~10 lines.
PROGRESS_STEP_PCT = 10
#: The sentence a start ended by /engine stop raises with (the lifecycle rewords it).
START_CANCELLED = "the start was cancelled by /engine stop"
#: Refuse to start when the card shows less than this free (see module note).
NINFER_MIN_FREE_MIB = 26 * 1024
#: `--max-concurrency N` — "valid range 1..8", default 1 (serving.md:760). Whether the
#: engine refuses or clamps a value outside it is unmeasured, so it is clamped here the
#: way the draft window is: the user's intent (more lanes) meets the widest legal value.
NINFER_CONCURRENCY_RANGE = (1, 8)

LITESUITE_LLM_DIR_ENV = "LITESUITE_LLM_DIR"


def litesuite_llm_dir() -> Path:
    override = os.environ.get(LITESUITE_LLM_DIR_ENV)
    return Path(override) if override else Path.home() / ".litesuite" / "llm"


def ninfer_executable(settings) -> Path:
    chosen = str(getattr(settings, "ninfer_executable", "") or "").strip()
    return Path(chosen) if chosen else litesuite_llm_dir() / "ninfer" / "ninfer-serve.exe"


def artifacts_dir() -> Path:
    """Where LiteSuite's Model Hub puts `.ninfer` containers."""
    return litesuite_llm_dir() / "ninfer-models"


def extra_artifact_roots() -> list[Path]:
    """`ninferArtifactRoots` in `~/.litesuite/llm/config.json` (T0839): more folders to
    scan for top-level `*.ninfer`. LiteSuite reads the same key the same way. Anything
    that is not a list of non-empty strings is ignored; a missing folder is skipped
    by the scan, not here."""
    try:
        body = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no file / bad JSON: no extra roots
        return []
    roots = body.get("ninferArtifactRoots") if isinstance(body, dict) else None
    if not isinstance(roots, list):
        return []
    return [Path(r.strip()) for r in roots if isinstance(r, str) and r.strip()]


def _same_path_key(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    return os.path.normcase(str(resolved))


def list_ninfer_artifacts() -> list[Path]:
    """Every `.ninfer` on disk, sorted. THE MODEL LIST FOR THIS BACKEND.

    🔴 ON NINFER THE MODEL IS NOT A CHOICE THE SERVER OFFERS. There is one
    artifact per process, fixed when the engine starts, so `/model`'s usual
    question — "which of the models this server has loaded?" — has no meaning
    here. The real question is which FILE the next engine will serve, and until
    T860 nothing in the TUI could ask it: `/model` and `/load` both list server
    models, and the only way to choose was typing a path into /settings.

    T0839: the default folder PLUS `ninferArtifactRoots` (config.json). Deduped by
    resolved path; a missing folder is ignored. Ryan's DeepSeek artifact lives on G:
    and the picker showed only the two in the default folder.

    Extracted so the picker and `ninfer_artifact` below cannot disagree about
    where artifacts live.
    """
    seen: set[str] = set()
    found: list[Path] = []
    for root in [artifacts_dir(), *extra_artifact_roots()]:
        if not root.is_dir():
            continue
        for path in root.glob("*.ninfer"):
            key = _same_path_key(path)
            if key not in seen:
                seen.add(key)
                if not is_drafter_only(path):
                    found.append(path)
    return sorted(found)


def is_drafter_only(path: Path) -> bool:
    """T1310: a DSpine drafter (`components` = only `dspine`) drafts beside a model and cannot be
    served alone, so it is not a choice in the model list. A file whose header does not answer
    stays listed."""
    return artifact_components(read_artifact_directory(path)) == ("dspine",)


def in_default_artifacts_dir(path: Path) -> bool:
    return _same_path_key(path.parent) == _same_path_key(artifacts_dir())


# ── model defaults: `<artifact>.launch.json` (T0839) ────────────────────────

MODEL_DEFAULTS_VERSION = 1


@dataclass(frozen=True)
class ModelDefaults:
    """One model's default settings: its `<artifact>.launch.json` file (read_model_defaults)."""
    path: Path
    #: None = the file names no build: its flags run on the user's configured engine (T1383).
    executable: Path | None
    model_id: str | None
    max_context: int | None
    args: tuple[str, ...]


def model_defaults_path(artifact: Path) -> Path:
    return artifact.with_name(artifact.name + ".launch.json")


def read_model_defaults(artifact: Path) -> ModelDefaults | None:
    """The artifact's model defaults (its `.launch.json` sidecar), or None when it has none.

    🔴 A SIDECAR THAT EXISTS IS ALWAYS OBEYED OR REFUSED, NEVER SKIPPED. One that does
    not parse (or fails the checks below) raises BackendError naming the file: falling
    back to the computed flags would start a different engine than the one the file
    describes (DeepSeek's model defaults REPLACE the app's tuning; their flags are measured)."""
    sidecar = model_defaults_path(artifact)
    if not sidecar.is_file():
        return None

    def bad(why: str) -> BackendError:
        return BackendError(f"the model defaults file {sidecar} is unusable: {why} — fix or delete it; nothing was started.")

    try:
        body = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise bad(f"cannot read it as JSON ({exc})") from exc
    if not isinstance(body, dict):
        raise bad("the top level is not an object")
    if body.get("version") != MODEL_DEFAULTS_VERSION:
        raise bad(f"version is {body.get('version')!r}, this build reads {MODEL_DEFAULTS_VERSION}")
    # T1383 (Ryan 2026-11-21: "stop trying to pin engines"): `executable` is optional. Absent or blank,
    # the file's flags run on the configured engine (ninfer_executable); named, exactly as before.
    exe = body.get("executable")
    if exe is not None and not isinstance(exe, str):
        raise bad("`executable` must be a path string (or left out to use the configured engine)")
    args = body.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise bad("`args` must be a list of strings")
    model_id = body.get("modelId")
    if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()):
        raise bad("`modelId` must be a non-empty string")
    max_context = body.get("maxContext")
    if max_context is not None and (not isinstance(max_context, int) or isinstance(max_context, bool) or max_context <= 0):
        raise bad("`maxContext` must be a positive integer")
    # T1310: upstream's DSpine drafter is an artifact of its own (`--spec dspine --draft-model PATH`).
    # A missing drafter would otherwise surface only at model load, after the weights are read.
    draft_model = _arg_value(args, "--draft-model")
    if "--draft-model" in args and not (isinstance(draft_model, str) and Path(draft_model).is_file()):
        raise bad(f"`--draft-model` names {draft_model!r}, which is not a file")
    if _arg_value(args, "--spec") == "dspine" and "--draft-model" not in args:
        raise bad("`--spec dspine` needs `--draft-model PATH` (the drafter is its own artifact)")
    return ModelDefaults(
        path=sidecar, executable=Path(exe.strip()) if exe and exe.strip() else None,
        model_id=model_id.strip() if model_id else None, max_context=max_context, args=tuple(args),
    )


# ── speed presets for DeepSeek's model defaults (Ryan 2026-11-08) ───────────
#
# Measured 2026-11-08 on the RTX 5090 / 96 GB box with the sync build (<scratch>/T1192/bench-*), DSpark, prefix reuse on, 131072 window.
# The host tier is how many of the 7,680 experts sit in pinned RAM (8.19 MiB each); the rest are read from the SSD on every prefill pass.
# Prefill times are first-token times on a cold prompt; run-to-run noise is about +-25%, so read the tiers as orders, not decimals.
#   old build, live session: 83K prompt 405 s (206 tok/s)
#   fastest  all 7,680 experts (62 GiB), chunk 4096: 32K prompt 17-21 s, 83K prompt 60-71 s, RAM ~80%, decode 45-49 tok/s
#   fast     5,621 experts (45 GiB), chunk 4096:      32K prompt 20-21 s, 83K prompt 51 s,    RAM ~63%
#   standard 5,621 experts (45 GiB), chunk 2048:      32K prompt 24 s,    83K prompt 54 s,    RAM ~63%
SPEED_PRESET_DEFAULT = "fastest"


@dataclass(frozen=True)
class SpeedPreset:
    key: str
    label: str
    host_mib: int | None        # --expert-host-mib; None leaves the model defaults' value
    prefill_chunk: int | None   # --prefill-chunk; None removes it (the engine's own 2048)
    needs_free_ram_gib: float   # fall back one tier when less than this is free at launch
    fallback: str | None


SPEED_PRESETS: dict[str, SpeedPreset] = {
    "fastest": SpeedPreset("fastest", "Fastest: all experts in RAM (62 GiB), chunk 4096", 63488, 4096, 70.0, "fast"),
    "fast": SpeedPreset("fast", "Fast: 45 GiB of experts in RAM, chunk 4096", 46080, 4096, 52.0, "standard"),
    "standard": SpeedPreset("standard", "Standard: 45 GiB of experts in RAM, chunk 2048", 46080, None, 0.0, None),
    # "recipe" is the value users' saved settings hold (ninfer_speed_preset), so the key stays; only its label changed (T1383-B).
    "recipe": SpeedPreset("recipe", "Model defaults as written (the .launch.json flags, untouched)", None, None, 0.0, None),
}


def cache_flags(settings) -> list[str]:
    """The opt-in context-cache flags the user switched on in Settings, both OFF by default."""
    out: list[str] = []
    if getattr(settings, "ninfer_auto_long_anchors", False) is True:
        out.append("--auto-long-anchors")
    if getattr(settings, "ninfer_derive_session_keys", False) is True:
        out.append("--derive-session-keys")
    return out


def available_ram_gib() -> float | None:
    """Free physical RAM in GiB, or None when Windows will not say. Never raises."""
    try:
        import ctypes

        class _Mem(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]

        mem = _Mem()
        mem.dwLength = ctypes.sizeof(_Mem)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
        return mem.ullAvailPhys / 2 ** 30
    except Exception:
        return None


def _set_flag(args: list[str], flag: str, value: str | None) -> list[str]:
    """`flag value` set to `value` (replaced in place, else appended); `value` None removes the pair."""
    out = list(args)
    if flag in out:
        i = out.index(flag)
        if value is None:
            del out[i:i + 2]
        elif i + 1 < len(out):
            out[i + 1] = value
        else:
            out.append(value)
    elif value is not None:
        out += [flag, value]
    return out


def apply_speed_preset(args: list[str], preset: str | None, *, free_ram_gib: float | None = None) -> tuple[list[str], str]:
    """The model defaults' args with the preset's two flags applied, and a one-line note for the log.

    Only model defaults that stream experts from disk (`--expert-residency disk`) are touched: any others (a Qwen artifact, residency host)
    has no expert tier to size. A tier whose RAM is not free at launch falls back one tier instead of failing to pin."""
    key = (preset or SPEED_PRESET_DEFAULT).strip().lower()
    chosen = SPEED_PRESETS.get(key)
    if chosen is None:
        return list(args), f"speed preset {key!r} unknown: the model defaults' flags are kept"
    if key == "recipe":
        return list(args), "speed preset: the model defaults as written"
    if "--expert-residency" not in args or args[args.index("--expert-residency") + 1:][:1] != ["disk"]:
        return list(args), f"speed preset {key}: not applied (this model's defaults have no disk expert tier)"
    note = ""
    while free_ram_gib is not None and chosen.needs_free_ram_gib > free_ram_gib and chosen.fallback:
        note += f"{chosen.key} needs {chosen.needs_free_ram_gib:.0f} GiB of free RAM, {free_ram_gib:.0f} free: "
        chosen = SPEED_PRESETS[chosen.fallback]
    out = _set_flag(args, "--expert-host-mib", str(chosen.host_mib) if chosen.host_mib else None)
    out = _set_flag(out, "--prefill-chunk", str(chosen.prefill_chunk) if chosen.prefill_chunk else None)
    return out, f"speed preset {note}{chosen.key}: --expert-host-mib {chosen.host_mib}, --prefill-chunk {chosen.prefill_chunk or 'engine default'}"


def build_model_defaults_args(
    artifact: Path, port: int, model_id: str, model_defaults: ModelDefaults, *,
    host: str = "127.0.0.1", user_max_context: int | None = None, speed_preset: str | None = None,
    free_ram_gib: float | None = None, on_note=None,
) -> list[str]:
    """The model defaults' argv, and NOTHING else: no kv-dtype, preserve-thinking, spec, vision,
    concurrency or host-kv from settings. Context precedence (Sentinel, T0839): the file's
    `maxContext`, else the user's `ninfer_max_context`, else the app default. The setting is
    global across artifacts; the model defaults are per-artifact and more specific."""
    if model_defaults.max_context:
        ctx = model_defaults.max_context
    elif isinstance(user_max_context, int) and not isinstance(user_max_context, bool) and user_max_context > 0:
        ctx = user_max_context
    else:
        ctx = NINFER_DEFAULT_MAX_CONTEXT
    tuned, note = apply_speed_preset(list(model_defaults.args), speed_preset, free_ram_gib=free_ram_gib)
    if on_note is not None:
        on_note(note)
    return [str(artifact), "--host", host, "--port", str(port), "--model-id", model_id,
            "--max-context", str(ctx), *tuned]


def effective_executable(settings) -> Path:
    """The binary the next start will run: the executable the chosen artifact's valid model
    defaults file names, else the setting / default (also for one that names none). Never raises (a
    status line calls it)."""
    try:
        art = ninfer_artifact(settings)
        model_defaults = read_model_defaults(art) if art is not None else None
    except BackendError:
        model_defaults = None
    return model_defaults.executable if model_defaults and model_defaults.executable else ninfer_executable(settings)


def ninfer_artifact(settings) -> Path | None:
    """The artifact to serve: the setting, else the ONE `.ninfer` LiteSuite pulled.

    Two candidates and no choice is None, not a guess — the wrong 22 GB file is
    a long wait that ends in the wrong model.

    ⚠️ AND THAT GUARD TOOK THE USER'S START PATH AWAY, SILENTLY, ON 2026-09-17.
    Two artifacts existed that morning; a conversion run wrote a third at 20:1x
    (qwen3_5_0_8b.ninfer). `len(found) == 1` stopped being true, so
    `/engine start` began refusing — hours before he tried it, for a reason
    that was nobody's mistake.

        THE GUARD IS RIGHT AND STAYS. What was wrong is that the exit it leaves
        had no door in the surface he was using. T860 gives `/model` the door;
        this function is unchanged.
    """
    chosen = str(getattr(settings, "ninfer_artifact", "") or "").strip()
    if chosen:
        return Path(chosen)
    found = list_ninfer_artifacts()
    return found[0] if len(found) == 1 else None


def read_artifact_directory(path: Path) -> dict | None:
    """The v3 container's JSON directory, or None when the header does not answer."""
    try:
        with open(path, "rb") as f:
            head = f.read(NINFER_V3_HEADER_BYTES)
            if len(head) < NINFER_V3_HEADER_BYTES or head[:8] != NINFER_V3_MAGIC:
                return None
            (json_bytes,) = struct.unpack_from("<Q", head, 8)
            if json_bytes <= 0 or json_bytes > NINFER_MAX_DIRECTORY_BYTES:
                return None
            body = f.read(json_bytes)
        directory = json.loads(body.decode("utf-8"))
        return directory if isinstance(directory, dict) else None
    except (OSError, ValueError):
        return None


#: The v3 payload starts at the directory's end rounded up to this (ninfer src/artifact/framing.h:18).
NINFER_PAYLOAD_ALIGNMENT = 4096
#: A text resource larger than this is not a chat template; refuse rather than read gigabytes.
NINFER_MAX_TEXT_RESOURCE_BYTES = 4 * 1024 * 1024


def read_artifact_text_resource(path: Path, role: str) -> str | None:
    """A text resource stored in the artifact (e.g. "chat_template.jinja"), or None.

    T1398: the served template is the authority for a model's effort scale, and NInfer's /props names
    the artifact (`model_path`) but not the template. The template is a `raw_bytes_v1` object listed
    under a component's `resources`; its bytes start at align_up(32 + json_bytes, 4096) + offset
    (ninfer src/artifact/reader.cpp:49). Reads the header and that one small object, never weights.
    Measured 2026-11-23 on the Inkling 2 Mini artifact: 6,868 bytes, byte-identical to its hf/ copy.
    """
    directory = read_artifact_directory(path)
    if not directory:
        return None
    comps = directory.get("components")
    objects = directory.get("objects")
    if not isinstance(comps, dict) or not isinstance(objects, list):
        return None
    ordered = sorted(comps.items(), key=lambda item: item[0] != "text")   # the text component first
    object_id = next((c["resources"][role] for _, c in ordered
                      if isinstance(c, dict) and isinstance(c.get("resources"), dict)
                      and isinstance(c["resources"].get(role), str)), None)
    entry = next((o for o in objects if isinstance(o, dict) and o.get("id") == object_id), None)
    if (entry is None or entry.get("encoding") != "raw_bytes_v1"
            or not isinstance(entry.get("offset"), int) or not isinstance(entry.get("bytes"), int)
            or not 0 < entry["bytes"] <= NINFER_MAX_TEXT_RESOURCE_BYTES):
        return None
    try:
        with open(path, "rb") as f:
            (json_bytes,) = struct.unpack_from("<Q", f.read(NINFER_V3_HEADER_BYTES), 8)
            base = -(-(NINFER_V3_HEADER_BYTES + json_bytes) // NINFER_PAYLOAD_ALIGNMENT) * NINFER_PAYLOAD_ALIGNMENT
            f.seek(base + entry["offset"])
            data = f.read(entry["bytes"])
        return data.decode("utf-8") if len(data) == entry["bytes"] else None
    except (OSError, ValueError, struct.error):
        return None


def artifact_components(directory: dict | None) -> tuple[str, ...]:
    """`components` is an OBJECT keyed text/vision/mtp/dflash2 (FellPeak, T788 delta)."""
    if not directory:
        return ()
    comps = directory.get("components")
    if isinstance(comps, dict):
        return tuple(str(k) for k in comps)
    if isinstance(comps, list):
        return tuple(str(k) for k in comps)
    return ()


def artifact_model_type(directory: dict | None) -> str | None:
    """The model family: `components.text.config.model_type` (`inkling2`, `qwen3_5_text`,
    `deepseek_v41` in the three artifacts read 2026-11-21), or None when the header does not say."""
    try:
        value = directory["components"]["text"]["config"]["model_type"]
    except (KeyError, TypeError, IndexError):
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def family_concurrency_cap(model_type: str | None, requested) -> int | None:
    """The lane count `model_type` is held to when `requested` is above it (NINFER_FAMILY_MAX_CONCURRENCY),
    else None: a family not listed, or a request within its limit, is sent unchanged."""
    rule = NINFER_FAMILY_MAX_CONCURRENCY.get(model_type or "")
    if rule is None or not isinstance(requested, int) or isinstance(requested, bool) or requested <= rule[0]:
        return None
    return rule[0]


def lanes_note(settings, requested) -> str:
    """One sentence for `/engine lanes N` when the chosen model will NOT get N lanes (a family rule on a
    start without model defaults), else "". Never raises: a status line calls it."""
    try:
        art = ninfer_artifact(settings)
        if art is None or not art.is_file() or read_model_defaults(art) is not None:
            return ""
    except BackendError:
        return ""
    family = artifact_model_type(read_artifact_directory(art))
    cap = family_concurrency_cap(family, requested)
    if cap is None:
        return ""
    return (f" {art.name} ({family}) starts with {cap} lane{'' if cap == 1 else 's'} instead: "
            f"{NINFER_FAMILY_MAX_CONCURRENCY[family][1]}. The model's defaults file can ask for more.")


def artifact_model_id(directory: dict | None, fallback: str = "qwen3.8-27b") -> str:
    """LiteSuite pins `qwen3.8-27b` when the header does not say (llm-handlers.ts:945)."""
    if directory:
        for key in ("model_id", "modelId", "name"):
            value = directory.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def build_ninfer_args(
    artifact: Path,
    port: int,
    model_id: str,
    *,
    host: str = "127.0.0.1",
    max_context: int | None = None,
    max_concurrency: int | None = None,
    spec: str | None = None,
    components: tuple[str, ...] = (),
    draft_tokens: int | None = None,
    kv_dtype: str = NINFER_DEFAULT_KV_DTYPE,
    kv_capacity: str | int | None = None,
    host_kv_mib: int | None = None,
    preserve_thinking: bool = True,
    vision: bool = True,
    model_type: str | None = None,
    fast_prefill_kernel: bool = False,
) -> list[str]:
    """LiteSuite's buildNInferArgs, flag for flag. Pure, so it has an arm.

    `model_type` picks the family's KV rule (NINFER_FAMILY_KV_DTYPES). `fast_prefill_kernel`
    is decided by `start` from the setting AND the engine's --help (engine_flags)."""
    args = [str(artifact), "--host", host, "--port", str(port), "--model-id", model_id]
    ctx = max_context if isinstance(max_context, int) and max_context > 0 else NINFER_DEFAULT_MAX_CONTEXT
    args += ["--max-context", str(ctx)]
    # Passed only when chosen (None = the engine's own default of 1), clamped into
    # NINFER_CONCURRENCY_RANGE. `bool` is an int; it is not a lane count.
    if isinstance(max_concurrency, int) and not isinstance(max_concurrency, bool) and max_concurrency > 0:
        lo, hi = NINFER_CONCURRENCY_RANGE
        lanes = min(hi, max(lo, max_concurrency))
        cap = family_concurrency_cap(model_type, lanes)
        if cap is None:
            args += ["--max-concurrency", str(lanes)]
        elif cap > 1:
            args += ["--max-concurrency", str(cap)]
        # else: held to one sequence -> no flag, the engine's own default.
    want = spec or NINFER_DEFAULT_SPEC
    # 🔴 gated on the ARTIFACT, not on the flag parser: the failure otherwise
    # lands at model load, after the user has waited (ninfer-args.ts).
    if want != "none" and want in components:
        lo, hi = NINFER_DRAFT_TOKEN_RANGE.get(want, (1, 3))
        chosen = draft_tokens if isinstance(draft_tokens, int) and draft_tokens > 0 else NINFER_DEFAULT_DRAFT_TOKENS
        args += ["--spec", want, "--draft-tokens", str(min(hi, max(lo, chosen))), "--lm-head-draft"]
    family_kv = NINFER_FAMILY_KV_DTYPES.get(model_type or "")
    if family_kv is None:
        args += ["--kv-dtype", kv_dtype if kv_dtype in NINFER_KV_DTYPES else NINFER_DEFAULT_KV_DTYPE]
    elif kv_dtype in family_kv:
        args += ["--kv-dtype", kv_dtype]
    # else: a type this family does not take -> no flag, the engine's own default for it.
    # `--kv-capacity N|auto` (serving.md:759, :949-952): omitted = follows --max-context,
    # the old behaviour. Anything that is neither "auto" nor a positive count is omitted.
    cap = str(kv_capacity).strip().lower() if kv_capacity is not None and not isinstance(kv_capacity, bool) else ""
    if cap == "auto" or (cap.isdigit() and int(cap) > 0):
        args += ["--kv-capacity", cap]
    # `--host-kv-mib N` (serving.md:786): pinned host KV; omitted = the engine's 8192.
    if isinstance(host_kv_mib, int) and not isinstance(host_kv_mib, bool) and host_kv_mib > 0:
        args += ["--host-kv-mib", str(host_kv_mib)]
    if fast_prefill_kernel:
        args.append("--fast-prefill-kernel")
    if preserve_thinking:
        args.append("--preserve-thinking")
    # 15:0x 2026-09-17, the user's screenshot: the 35B-A3B carries a `vision` component, the
    # engine was started without --vision, and the first view_image put a media part in the
    # history -> every request after it was `HTTP 400 vision disabled` (serving.md:43-45).
    # Gated on the artifact like --spec: the flag on a text-only artifact fails at load.
    if vision and "vision" in components:
        args.append("--vision")
    return args


def loop_kv_args(args, family: str | None, supported) -> list[str]:
    """`["--loop-kv", MODE]` when a start of `family` should carry it, else [].

    The flag is added only for a family in NINFER_FAMILY_LOOP_KV, only when `supported` (the engine's
    `--help` options, engine_flags) lists it, and never when `args` already name `--loop-kv`: a model
    defaults file that sets it (or sets another mode) is the more specific word and is not fought.
    An engine without the flag simply is not asked for it; that is the engine lacking the option, not
    a second mode."""
    mode = NINFER_FAMILY_LOOP_KV.get(family or "")
    if mode is None or "--loop-kv" in args or "--loop-kv" not in supported:
        return []
    return ["--loop-kv", mode]


def concurrency_in(args) -> int:
    """The `--max-concurrency` an argv carries, or 1 — the engine's default when the
    flag is absent (serving.md:760)."""
    val = _arg_value(list(args or ()), "--max-concurrency")
    return int(val) if isinstance(val, str) and val.isdigit() else 1


def running_argv(port: int) -> list[str] | None:
    """The command line of the ninfer-serve listening on `port`, from the process
    table; None when none is visible (nothing running, or a host that is not this box).

    ⬜ THE PROCESS TABLE, NOT THE SETTING. An ATTACHED engine was started by somebody
    else with THEIR flags, and `/v1/models` does not carry capacity — so the only
    honest source for "how many lanes is it running" is the argv of the live process.
    Same query summarize.py's find_port used the day a brief's port went stale.
    """
    ps = "Get-CimInstance Win32_Process -Filter \"Name='ninfer-serve.exe'\" | % { $_.CommandLine }"
    try:
        out = ttyguard.run(["powershell", "-NoProfile", "-Command", ps], timeout=30)
        text = str(getattr(out, "stdout", out) or "")
    except Exception:  # noqa: BLE001 - cannot ask -> unknown, never a guess
        return None
    for line in text.splitlines():
        toks = line.split()
        if _arg_value(toks, "--port") == str(port):
            return toks
    return None


#: An option line of `ninfer-serve --help`: `  --fast-prefill-kernel   prefill an int8 ...`, optionally
#: after a short alias (`  -h, --help`). Only the line's FIRST long option counts, so a flag merely
#: named inside another option's description is not read as supported. Option lines are indented 2 or 4;
#: a wrapped description is indented ~32 and can BEGIN with an option name ("--draft-tokens or half of
#: them"), so the indent is held to 1-8.
_HELP_OPTION_RE = re.compile(r"^[ \t]{1,8}(?:-[A-Za-z0-9],[ \t]*)?(--[A-Za-z0-9][A-Za-z0-9_-]*)", re.M)
NINFER_HELP_TIMEOUT_S = 15
#: (normcased resolved path, size, mtime_ns) -> the long options that build's --help lists. Keyed on
#: size and mtime too, so an engine rebuilt at the same path is asked again.
_ENGINE_FLAGS: dict[tuple[str, int, int], frozenset[str]] = {}


def engine_flags(exe: Path, *, run=None) -> frozenset[str]:
    """The long options `exe --help` lists, read once per build (T1383).

    Ryan 2026-11-21: "this isnt a private app" — an optional flag ships for every user, on
    whatever engine they run, so LiteTUI asks the engine instead of naming builds. Any failure
    (missing file, timeout, non-zero exit, nothing parseable) is an EMPTY set and never raises:
    an engine LiteTUI cannot question gets no optional flags. A failure is not cached, so a
    one-off timeout does not switch a flag off for the life of the process. `run` defaults to
    `ttyguard.run` (no console window); tests inject it."""
    try:
        st = exe.stat()
        key = (os.path.normcase(str(exe.resolve())), st.st_size, st.st_mtime_ns)
    except OSError:
        return frozenset()
    cached = _ENGINE_FLAGS.get(key)
    if cached is not None:
        return cached
    try:
        out = (run or ttyguard.run)([str(exe), "--help"], timeout=NINFER_HELP_TIMEOUT_S, cwd=str(exe.parent))
        if getattr(out, "returncode", 0) != 0:
            return frozenset()
        text = f"{getattr(out, 'stdout', '') or ''}\n{getattr(out, 'stderr', '') or ''}"
        flags = frozenset(_HELP_OPTION_RE.findall(text))
    except Exception:  # noqa: BLE001 - an engine we cannot question gets no optional flags
        return frozenset()
    if flags:
        _ENGINE_FLAGS[key] = flags
    return flags


def free_port(host: str = "127.0.0.1") -> int:
    """A port nothing is listening on, from the OS rather than from a guess."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def engine_process_alive() -> bool:
    """Is any ninfer-serve process running? (tasklist; True on doubt so we never double-spawn)."""
    try:
        out = ttyguard.run(["tasklist", "/FI", "IMAGENAME eq ninfer-serve.exe", "/NH"], timeout=10)
        text = str(getattr(out, "stdout", out))
        return "ninfer-serve" in text.lower()
    except Exception:  # noqa: BLE001 - cannot ask -> assume alive, refuse to spawn
        return True


def gpu_free_mib() -> int | None:
    """`nvidia-smi` free memory, or None when it cannot be asked (then we do not refuse on it)."""
    try:
        out = ttyguard.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], timeout=10
        )
        text = out.stdout if hasattr(out, "stdout") else str(out)
        return int(str(text).strip().splitlines()[0].strip())
    except Exception:  # noqa: BLE001 - absent tool is "unknown", not "refuse"
        return None


# ── the shared registry: LiteSuite's extraEndpoints ─────────────────────────

def _config_path() -> Path:
    return litesuite_llm_dir() / "config.json"


def registered_entry() -> dict | None:
    """The WHOLE ninfer entry, `owner` and `pid` included — not just its URL.

    🔴 `register_host` WRITES `owner` AND `pid`, AND EVERY READER USED TO THROW
    THEM AWAY. `stop_engine` re-read this file through a URL-only reader, found
    no in-memory `OwnedEngine`, and told the user *"the engine at … was not
    started by LiteTUI — stop it where it was started"* — about a record
    LiteTUI had written itself, saying `owner: "litetui"`, with the pid beside
    it.

    THE USER, 2026-09-18: *"it spawned that ninfer server then refused to close it
    with /engine stop saying it didnt spawn it ... killing litetui didnt close
    the server"*. Measured: ninfer-serve.exe pid 269276, 10.7 GB resident,
    parent already gone, entry still naming litetui as owner.

    The in-memory handle answers "did THIS PROCESS start it". The registry
    answers "did LiteTUI start it", which is the question the user is asking
    and survives the restart that loses the handle.
    """
    try:
        body = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no file, no entry, bad JSON: all mean "no engine"
        return None
    for entry in body.get("extraEndpoints") or []:
        if isinstance(entry, dict) and entry.get("kind") == "ninfer":
            base = entry.get("baseUrl")
            if isinstance(base, str) and base.strip():
                return {**entry, "baseUrl": base.rstrip("/")}
    return None


def registered_host() -> str | None:
    """Same read as ninfer_backend.discover_ninfer_host, kept here so the launcher
    can refuse on it without importing the backend."""
    entry = registered_entry()
    return entry["baseUrl"] if entry else None


def process_identity(pid) -> dict | None:
    """What makes a recorded pid safe to adopt later, read from the live process at spawn:
    its creation time and its executable path. None when it cannot be read (the record is
    then written without them, and a later session refuses to adopt it, saying so)."""
    try:
        import psutil
        proc = psutil.Process(pid)
        with proc.oneshot():
            return {"created": round(proc.create_time(), 3), "exe": proc.exe()}
    except Exception:  # noqa: BLE001 - gone, denied, no psutil: all mean "no identity"
        return None


@dataclass
class AdoptOutcome:
    """What `stop_adopted` did. `freed` = no engine of ours is left at the record (stopped
    now, or already gone and its record cleared). `reason` names what blocked a stop."""
    freed: bool
    message: str = ""
    reason: str = ""


#: Two reads of one process's creation time agree to the millisecond; a reused pid is a
#: process created later. The tolerance covers rounding of the recorded value only.
_CREATED_TOLERANCE_S = 0.01
#: How long a tree-kill gets to show that the engine and its children are gone.
_ADOPT_EXIT_WAIT_S = 10.0


def _same_exe(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _listening_ports(proc) -> set[int] | None:
    import psutil
    try:
        conns = proc.net_connections(kind="tcp") if hasattr(proc, "net_connections") else proc.connections(kind="tcp")
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return None
    return {c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN and c.laddr}


def stop_adopted(entry: dict, *, healthy) -> AdoptOutcome:
    """Stop an engine a PREVIOUS LiteTUI session started (another process, or this one
    before /agent-new): Ryan 2026-11-14, *"its refusting to stop ninfer after slash
    agent-new"*.

    A pid alone may be REUSED, so the record written at spawn also carries the process's
    creation time and exe path (`process_identity`), and its baseUrl names the port. Every
    one is checked against the live process: same pid, same creation time, same exe, and
    that process listens on the recorded port. All match: the pid is our engine, and it gets
    the tree-kill an owned engine without a job gets (taskkill /T), then the exit is proved
    and only then the record is removed. Any one differs: nothing is killed, the record is
    kept, and the reason names which.

    A recorded pid that no longer exists, with nothing answering at the port, is an engine
    that already exited: its record (that pid, that URL) is cleared."""
    pid = entry.get("pid")
    if entry.get("owner") != "litetui" or entry.get("kind") != "ninfer":
        return AdoptOutcome(False, reason="the record was not written by LiteTUI")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return AdoptOutcome(False, reason="the record has no valid pid")
    base = str(entry.get("baseUrl") or "").rstrip("/")
    missing = [k for k in ("created", "exe") if not entry.get(k)]
    if missing:
        return AdoptOutcome(False, reason=f"the record has no {' or '.join('creation time' if k == 'created' else 'exe path' for k in missing)} "
                                          "(it was written by an older LiteTUI)")
    from urllib.parse import urlsplit
    try:
        port = urlsplit(base).port
    except ValueError:
        port = None
    if not port:
        return AdoptOutcome(False, reason=f"the record's address {base!r} has no port")
    try:
        import psutil
    except ImportError:
        return AdoptOutcome(False, reason="psutil is not available to read the process")
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            created, exe = proc.create_time(), proc.exe()
    except psutil.NoSuchProcess:
        if not _clear_exited_record(base, pid, healthy=healthy):
            return AdoptOutcome(False, reason=f"pid {pid} no longer exists, but something answers at {base}")
        return AdoptOutcome(True, f"the engine LiteTUI started at {base} (pid {pid}) has already exited; its record is cleared.")
    except psutil.AccessDenied:
        return AdoptOutcome(False, reason=f"access to pid {pid} was denied")
    if abs(created - float(entry["created"])) > _CREATED_TOLERANCE_S:
        return AdoptOutcome(False, reason=f"pid {pid}'s creation time differs from the record "
                                          f"({time.strftime('%F %T', time.localtime(created))} now, "
                                          f"{time.strftime('%F %T', time.localtime(float(entry['created'])))} recorded): the pid was reused")
    if not _same_exe(exe, str(entry["exe"])):
        return AdoptOutcome(False, reason=f"pid {pid} runs {exe}, not the recorded {entry['exe']}: the pid was reused")
    ports = _listening_ports(proc)
    if ports is None:
        return AdoptOutcome(False, reason=f"pid {pid}'s listening ports could not be read")
    if port not in ports:
        return AdoptOutcome(False, reason=f"pid {pid} is not listening on port {port}")
    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    _log_event(f"adopting the engine at {base} (pid {pid}: creation time, exe and port match) to stop it")
    try:
        ttyguard.run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass
    _gone, alive = psutil.wait_procs([proc, *children], timeout=_ADOPT_EXIT_WAIT_S)
    if proc in alive:
        return AdoptOutcome(False, reason=f"pid {pid} matched and was sent a tree kill, but its exit was NOT confirmed (it may still be running)")
    if not _remove_entry(base, pid):
        return AdoptOutcome(True, f"stopped the engine LiteTUI started at {base} (pid {pid}), but its record could not be removed.")
    left = [p.pid for p in alive]
    if left:
        return AdoptOutcome(True, f"stopped the engine LiteTUI started at {base} (pid {pid}); "
                                  f"{len(left)} child process(es) are still running: {', '.join(map(str, left))}.")
    return AdoptOutcome(True, f"stopped the engine LiteTUI started at {base} (pid {pid}) — it and its "
                              f"{len(children)} child process(es) are confirmed gone.")


def _clear_exited_record(base: str, pid: int, *, healthy) -> bool:
    """Remove the record of an engine whose pid no longer exists. False, record kept, when
    something still answers at `base`. The one clear `stop_adopted` and `clear_dead_record` share."""
    if healthy(base):
        return False
    _remove_entry(base, pid)
    _log_event(f"cleared the record of an engine that already exited ({base}, pid {pid})")
    return True


def clear_dead_record(entry: dict | None, *, healthy) -> bool:
    """T1315: clear a record LiteTUI wrote for an engine that is gone, without killing anything.

    Only when ALL hold: owner is litetui, kind is ninfer, the pid does not exist and nothing
    answers at its baseUrl. A pid that exists (ours or not, reused or not) keeps the record,
    and so does an answering port or any record another owner wrote. True when cleared."""
    if not isinstance(entry, dict) or entry.get("owner") != "litetui" or entry.get("kind") != "ninfer":
        return False
    pid = entry.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    base = str(entry.get("baseUrl") or "").rstrip("/")
    if not base:
        return False
    try:
        import psutil
    except ImportError:
        return False
    try:
        psutil.Process(pid)
    except psutil.NoSuchProcess:
        return _clear_exited_record(base, pid, healthy=healthy)
    except psutil.Error:                      # AccessDenied: it exists
        return False
    return False


def clear_exited_owned(owned) -> bool:
    """T1315: this session's own engine process exited without /engine stop (it crashed, or
    was killed): remove its record. Quiet and repeatable: it acts only while a record still
    names this handle's host and pid, so a status or start poll can call it every time."""
    if not getattr(owned, "registered", True):
        return False
    poll = getattr(owned.proc, "poll", None)
    if poll is None or poll() is None:
        return False
    host, pid = str(owned.host).rstrip("/"), getattr(owned.proc, "pid", None)
    entry = registered_entry()
    if not entry or entry.get("owner") != "litetui" or entry.get("pid") != pid or entry["baseUrl"] != host:
        return False
    if not _remove_entry(host, pid):
        return False
    _log_event(f"cleared the record of this session's engine that exited on its own ({host}, pid {pid})")
    return True


def stop_registered(entry: dict) -> bool:
    """True only when `stop_adopted` stopped the engine. A record without a verified identity
    (an older LiteTUI's, or a foreign or malformed one) is never killed and never dropped: a
    pid-only `taskkill` could hit a reused pid (option A, 2026-09-21)."""
    return stop_adopted(entry, healthy=lambda _h: True).freed


def register_host(base_url: str, *, pid: int | None = None, identity: dict | None = None) -> bool:
    """Add `{baseUrl, kind: ninfer, owner: "litetui", pid}` to LiteSuite's config; False when
    the file is not writable. `owner`/`pid` are the vocabulary NeonRack and NeonRelay agreed for
    the Model Hub (T819/T820, 2026-09-17): the hub labels an answering engine LiteSuite | LiteTUI
    | external by WHO WROTE THE ENTRY and may only Stop its own; an entry without `owner` reads
    as external. Every reader keys on baseUrl + kind only, so older readers are unaffected."""
    path = _config_path()
    try:
        with _registry_lock():                       # cross-process RMW lock (OwnershipError -> False)
            body = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"version": 1}
            if not isinstance(body, dict):
                body = {"version": 1}
            entries = [e for e in (body.get("extraEndpoints") or []) if not (isinstance(e, dict) and e.get("kind") == "ninfer")]
            entry: dict = {"baseUrl": base_url, "kind": "ninfer", "owner": "litetui"}
            if pid is not None:
                entry["pid"] = pid
                if identity:                           # what lets a later session adopt it (stop_adopted)
                    entry.update({k: identity[k] for k in ("created", "exe") if identity.get(k)})
            entries.append(entry)
            body["extraEndpoints"] = entries
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
    except (OSError, ValueError):
        return False


def unregister_host(base_url: str) -> None:
    """Legacy URL-only removal (a stale-cleanup caller). Still a registry writer, so it holds
    the same cross-process lock around its read-modify-write; a contended lock is a silent
    no-op (the entry stays for the next attempt)."""
    path = _config_path()
    try:
        with _registry_lock():                       # cross-process RMW lock (all writers share it)
            body = json.loads(path.read_text(encoding="utf-8"))
            entries = body.get("extraEndpoints") or []
            body["extraEndpoints"] = [
                e for e in entries
                if not (isinstance(e, dict) and e.get("kind") == "ninfer" and str(e.get("baseUrl", "")).rstrip("/") == base_url.rstrip("/"))
            ]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
            os.replace(tmp, path)
    except (OSError, ValueError):
        pass


# ── the process we own ──────────────────────────────────────────────────────

@dataclass
class OwnedEngine:
    proc: object
    host: str
    log_path: Path
    log_file: object
    model_id: str
    #: A KILL_ON_JOB_CLOSE job holding the engine. THE POINT IS WHAT HAPPENS
    #: WHEN NOBODY RUNS ANY CODE: Windows closes a dead process's handles, so
    #: the engine dies with LiteTUI even when LiteTUI is FORCE-KILLED and no
    #: `atexit` ever runs. That is the case the user hit on 2026-09-18 —
    #: *"killing litetui didnt close the server"* — leaving ninfer-serve.exe
    #: pid 269276 resident with 10.7 GB. None on non-Windows or if the job
    #: could not be made; `stop` then falls back to the taskkill walk.
    job: int | None = None
    #: The argv it was spawned with. `/engine status` and `seat_snapshot` read the
    #: running capacity from HERE for an owned engine — the flags are startup-only
    #: and no route reports them (ninfer http_server.cpp:480-497 passes id, time and
    #: max_context only), so the setting is not the answer; the argv is.
    args: tuple[str, ...] = ()
    #: False for an engine that is NOT in LiteSuite's `extraEndpoints` registry (Strata:
    #: its port is a setting of the install, so nothing is registered and nothing to remove).
    registered: bool = True

    @property
    def alive(self) -> bool:
        poll = getattr(self.proc, "poll", None)
        return poll is not None and poll() is None


def _log_tail(path: Path, lines: int = 5) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    return " ".join(text.strip().splitlines()[-lines:])[:400]


def log_path() -> Path:
    from .paths import data_root
    return data_root() / ".ninfer" / "litetui-ninfer-serve.log"


def _log_event(text: str) -> None:
    """Append one timestamped line to the engine log, for outcomes that never spawn.

    🔴 T877. A REFUSED `/engine start` USED TO LEAVE NO TRACE ANYWHERE. Every
    refusal raises above the `open(lp, "a")` in `start()`, so the log — the one
    artefact anybody looks at afterwards — was written ONLY on the paths that got
    as far as launching a process. Measured 2026-09-18 00:2x: the user ran `/model`
    then `/engine start`, nothing happened, and the investigation had NOTHING to
    read: no log line, no process, no registry entry, no VRAM movement. Three
    agents guessed at four different causes for twenty minutes because the only
    record of the refusal was a chat bubble that had scrolled away.

        A FAILURE THAT WRITES NOTHING IS INDISTINGUISHABLE FROM A COMMAND THAT
        NEVER RAN — and those two need completely different fixes.

    Best-effort by construction: a logging failure must never be the reason a
    start fails, so every error here is swallowed. The refusal still reaches the
    user through the raised BackendError; this is the copy that survives.
    """
    try:
        lp = log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"--- litetui {time.strftime('%F %T')} | {text}\n")
    except OSError:
        pass


def registration_state(reg: str | None, *, healthy) -> str:
    """What the registered engine at `reg` is: "none", "answering", "loading" (silent, but a
    ninfer-serve process exists) or "cleared" (silent and NO process: a dead entry, removed here).

    THE ONE PLACE A DEAD REGISTRATION IS CLEARED (T1311). `refuse_reason` and the backend's
    `ensure_running` both ask it. Ryan, 2026-11-14, after LiteTUI told him an engine at :54149
    "may still be loading weights" when no ninfer process existed and nothing listened there:
    "Clear dead engines". Measured 2026-09-17 13:2x as well: a holder killed with
    TerminateProcess never ran its atexit, so the dead port stayed in LiteSuite's config.
    """
    if reg is None:
        return "none"
    if healthy(reg):
        return "answering"
    entry = registered_entry()
    if entry and entry["baseUrl"] == reg.rstrip("/") and clear_dead_record(entry, healthy=lambda _h: False):
        return "cleared"                      # T1315: its own pid is gone, whatever else is running
    if engine_process_alive():
        return "loading"
    unregister_host(reg)
    _log_event(f"cleared a dead registration at {reg} (silent on /health, no ninfer-serve process)")
    return "cleared"


def refuse_reason(settings, *, healthy) -> str | None:
    """Why a start must NOT happen, or None. Every path that puts a model in VRAM
    answers here first (the user 2026-09-10: check, ask, then load)."""
    explicit = str(getattr(settings, "ninfer_host", "") or "").strip().rstrip("/")
    if explicit and healthy(explicit):
        return f"an NInfer engine is already answering at {explicit} (ninfer_host) — attach to it; nothing was started."
    reg = registered_host()
    state = registration_state(reg, healthy=healthy)
    if state == "answering":
        return f"an NInfer engine is already registered and answering at {reg} — attach to it; nothing was started."
    if state == "loading":
        # A process exists and is not answering yet: it is LOADING (weights take
        # ~8 s, the port binds after). Starting a second one now is the VRAM bug.
        return (f"an NInfer engine is registered at {reg} and still loading (a ninfer-serve process "
                "exists) — wait for it, then attach; nothing was started.")
    free = gpu_free_mib()
    if free is not None and free < NINFER_MIN_FREE_MIB:
        return (f"only {free} MiB free on the GPU; the engine needs about {NINFER_MIN_FREE_MIB} MiB "
                "(weights + runtime + 32k fp8 KV). Something else holds the card — nothing was started.")
    return None


def start(settings, *, healthy, spawn=ttyguard.popen, notice=None, progress=None,
          cancel=None, help_run=None) -> OwnedEngine:
    """Spawn ninfer-serve with LiteSuite's argv, wait for its ready line, register it.

    `healthy(host) -> bool` and `spawn` are injected so the arms never touch the
    real card. Raises BackendError with the reason on every refusal or failure.

    `progress(line)` gets the weight-load lines while it waits; `cancel` (a threading.Event)
    set by /engine stop ends the wait and tree-kills what was spawned (T1311).

    `notice()` is called ONCE, immediately before the spawn, for a caller that
    wants to say "this will take a moment". It is here rather than in the caller
    because ONLY THIS FUNCTION KNOWS THE START IS ACTUALLY HAPPENING (T865): five
    conditions refuse above this line, and a caller announcing a start before
    asking about them tells the user something the next line contradicts.
    """
    # T877 — every outcome below writes to the log, including the ones that never
    # spawn. `_log_event`'s docstring carries the incident that earned this.
    _log_event("engine start requested")
    reason = refuse_reason(settings, healthy=healthy)
    if reason:
        _log_event(f"REFUSED: {reason}")
        raise BackendError(reason)
    # T0839: the artifact's model defaults (if any) name their own executable, so they are read
    # before the executable check. A defaults file that is present and unusable refuses here.
    artifact = ninfer_artifact(settings)
    try:
        model_defaults = read_model_defaults(artifact) if artifact is not None and artifact.is_file() else None
    except BackendError as exc:
        _log_event(f"REFUSED: {exc}")
        raise
    exe = model_defaults.executable if model_defaults and model_defaults.executable else ninfer_executable(settings)
    if not exe.is_file():
        if model_defaults and model_defaults.executable:
            msg = f"the model defaults file {model_defaults.path} names {exe}, which does not exist — fix the file; nothing was started."
        else:
            msg = f"ninfer-serve not installed at {exe} — install it from LiteSuite's Model Hub, or set ninfer_executable."
        _log_event(f"REFUSED: {msg}")
        raise BackendError(msg)
    if artifact is None or not artifact.is_file():
        msg = "no NInfer artifact chosen — set ninfer_artifact to a .ninfer file (LiteSuite's Model Hub pulls one)."
        _log_event(f"REFUSED: {msg} (ninfer_artifact={getattr(settings, 'ninfer_artifact', None)!r})")
        raise BackendError(msg)
    directory = read_artifact_directory(artifact)
    # 14:2x 2026-09-17: the 35B-A3B header carries no model_id; the pinned fallback made the
    # running MoE advertise itself as qwen3.8-27b in every picker. The file's own name is
    # the honest label when the header says nothing.
    model_id = artifact_model_id(directory, fallback=artifact.stem)
    if model_defaults and model_defaults.model_id:
        model_id = model_defaults.model_id   # the id a client sees (/model, conversations)
    port = free_port()
    host = f"http://127.0.0.1:{port}"
    ctx = getattr(settings, "ninfer_max_context", None)
    conc = getattr(settings, "ninfer_max_concurrency", None)
    if model_defaults:
        args = build_model_defaults_args(artifact, port, model_id, model_defaults,
                                         user_max_context=ctx if isinstance(ctx, int) else None,
                                         speed_preset=getattr(settings, "ninfer_speed_preset", None),
                                         free_ram_gib=available_ram_gib(), on_note=_log_event)
        # The cache flags LiteTUI adds to a model defaults start go through the engine's --help (T1383):
        # model defaults with no executable run on the configured engine, which may be too old for them.
        # When the help cannot be read, a defaults file that names its own build keeps today's behaviour
        # (it was written for that build); one that names none gets none of them.
        wanted = [f for f in cache_flags(settings) if f not in args]
        if wanted:
            supported = engine_flags(exe, run=help_run)
            if supported:
                keep = [f for f in wanted if f in supported]
            else:
                keep = wanted if model_defaults.executable else []
            dropped = [f for f in wanted if f not in keep]
            if dropped:
                why = "its --help does not list them" if supported else "its --help could not be read"
                _log_event(f"{' '.join(dropped)} left out: {exe} {why}; the model defaults' own flags are unchanged")
            args += keep
        # T1383: no --fast-prefill-kernel here. A model defaults file is one model's measured flag set
        # (DeepSeek with disk experts); the kernel was not measured there, so its flags stay as written.
    else:
        fast_prefill = False
        if getattr(settings, "ninfer_fast_prefill_kernel", True) is True:
            supported = engine_flags(exe, run=help_run)
            fast_prefill = "--fast-prefill-kernel" in supported
            if not fast_prefill:
                why = "its --help does not list it" if supported else "its --help could not be read"
                _log_event(f"--fast-prefill-kernel left out: {exe} {why}; starting with the standard kernel")
        family = artifact_model_type(directory)
        cap = family_concurrency_cap(family, conc if isinstance(conc, int) else None)
        if cap is not None:
            _log_event(f"--max-concurrency {conc} lowered to {cap} for model family {family} (no model defaults file): "
                       f"{NINFER_FAMILY_MAX_CONCURRENCY[family][1]}")
        args = build_ninfer_args(
            artifact, port, model_id,
            max_context=ctx if isinstance(ctx, int) else None,
            max_concurrency=conc if isinstance(conc, int) else None,
            components=artifact_components(directory),
            kv_dtype=getattr(settings, "ninfer_kv_dtype", None) or NINFER_DEFAULT_KV_DTYPE,
            kv_capacity=getattr(settings, "ninfer_kv_capacity", None),
            host_kv_mib=getattr(settings, "ninfer_host_kv_mib", None),
            model_type=family,
            fast_prefill_kernel=fast_prefill,
        )
    # LOOPKV-466: Inkling 2 starts with the shared loop KV on, on either path above. Asked only for a
    # family that takes it; the engine's --help decides whether this build has the option.
    loop_family = artifact_model_type(directory)
    if loop_family in NINFER_FAMILY_LOOP_KV and "--loop-kv" not in args:
        extra = loop_kv_args(args, loop_family, engine_flags(exe, run=help_run))
        if extra:
            args += extra
        else:
            _log_event(f"--loop-kv left out: {exe} its --help does not list it (or could not be read)")
    lp = log_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(lp, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - the process writes here for its whole life
    note = f" (model defaults {model_defaults.path.name})" if model_defaults else ""
    log_file.write(f"\n--- litetui spawn {time.strftime('%F %T')}{note} ---\n{exe} {' '.join(args)}\n")
    log_file.flush()
    # 14:1x 2026-09-17: the log is APPENDED across spawns, so a marker from an earlier run
    # must not count — only bytes written after this spawn's header are this engine's.
    start_off = lp.stat().st_size
    if cancel is not None and cancel.is_set():
        log_file.close()
        _log_event(f"REFUSED: {START_CANCELLED} before anything was spawned")
        raise EngineStartFailed(START_CANCELLED)
    if notice is not None:
        notice()
    proc = spawn(
        [str(exe), *args],
        stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
        cwd=str(exe.parent),  # the CUDA DLLs live beside the exe
    )
    # 🔴 ADOPT IT INTO A KILL_ON_JOB_CLOSE JOB, IMMEDIATELY — before the
    # readiness wait, so an engine that hangs while loading 10 GB of weights is
    # covered too, not only one that came up.
    #
    # `atexit` was the only cleanup here, and atexit does not run when the app
    # is force-killed. the user, 2026-09-18: *"killing litetui didnt close the
    # server"* — ninfer-serve.exe pid 269276, 10.7 GB, parent gone. A job needs
    # nobody to run anything: the kernel kills every member when the last
    # handle closes, and Windows closes our handles for us when we die however
    # we die. Descendants inherit the job, so there is nothing to enumerate.
    #
    # `jobkill` already existed for `ttyguard` and `lifecycle_hooks`; this
    # spawn simply never used it.
    job = jobkill.create()
    pid = getattr(proc, "pid", None)
    # Read now, while the process is certainly ours: a later session adopts it only if the
    # pid still has this creation time and exe (stop_adopted).
    identity = process_identity(pid) if isinstance(pid, int) else None
    if job is not None and isinstance(pid, int):
        if not jobkill.assign(job, pid):
            # Not fatal: `stop` still has the taskkill path. But say so, or a
            # later orphan looks like the job silently failing to hold.
            _log_event(f"job assign FAILED for pid {pid} — orphan protection is off for this engine")
            jobkill.close(job)
            job = None
    owned = OwnedEngine(proc=proc, host=host, log_path=lp, log_file=log_file,
                        model_id=model_id, job=job, args=tuple(args))
    outcome, fresh = wait_for_ready(lp, start_off, proc, host, healthy=healthy,
                                     ready_marker=NINFER_READY_MARKER, stall_s=NINFER_START_TIMEOUT_S,
                                     interval=0.5, failure_markers=NINFER_FAILURE_MARKERS,
                                     cancel=cancel, progress=progress)
    if outcome == "ready":
        register_host(host, pid=pid, identity=identity)
        atexit.register(stop, owned)
        return owned
    if outcome == "cancelled":
        _log_event(START_CANCELLED)
        _fail_start(owned, START_CANCELLED)
    # T1338/T1339: the user reads ONE plain sentence from the engine's ERROR lines. The raw
    # lines (timestamps, device profile, byte counts) stay in the log file.
    if outcome.startswith("failed:") or outcome == "exited":
        said, reason = engine_words.explain_failure(fresh, args)
        if reason is None:
            if outcome.startswith("failed:") and not engine_words.error_lines(fresh):
                said = outcome[7:]          # the marker is the only reason the engine gave
            said = f"ninfer-serve could not start: {said}. Engine log: {lp}"
        _log_event(f"start failed ({outcome}): {said}")
        _fail_start(owned, said, reason=reason)
    _fail_start(owned, f"ninfer-serve did not become ready: it printed nothing new for "
                       f"{NINFER_START_TIMEOUT_S}s. Engine log: {lp}")


def _progress_key(line: str):
    """What makes a progress line worth reporting: its 10 % step while weights load,
    otherwise the line itself (each phase is said once)."""
    if line.lower().startswith("loading weights"):
        m = _PERCENT_RE.search(line)
        if m:
            return ("weights", int(float(m.group(1)) // PROGRESS_STEP_PCT))
    return line.lower()


def latest_progress(fresh: str) -> str | None:
    """The newest progress line in this spawn's output, or None. A progress bar may
    rewrite itself with `\\r`, so lines are split on both."""
    found = _PROGRESS_RE.findall(fresh.replace("\r", "\n"))
    return found[-1].strip() if found else None


def wait_for_ready(lp, start_off, proc, host, *, healthy, ready_marker, stall_s, interval,
                   failure_markers=(), cancel=None, progress=None) -> tuple[str, str]:
    """Wait for THIS spawn's ready line; the start wait every launcher shares (T1311).

    Returns `(outcome, fresh)`: "ready", "failed:<marker>", "exited", "stalled" (no new
    output for `stall_s`) or "cancelled" (`cancel` was set by /engine stop). `progress(line)`
    is called from this thread for each new progress step (see `_progress_key`).

    🔴 FOLLOWS PROGRESS, NOT A WALL. Every new byte restarts the stall window, so a 58 GB
    read that prints a line every few seconds is never cut off; one that prints nothing is.
    """
    last_size, last_growth, reported = -1, time.monotonic(), None
    fresh = ""
    while True:
        if cancel is not None and cancel.is_set():
            return "cancelled", fresh
        try:
            with open(lp, "rb") as f:
                f.seek(start_off)
                fresh = f.read().decode("utf-8", "replace")
        except OSError:
            pass
        now = time.monotonic()
        if len(fresh) != last_size:
            last_size, last_growth = len(fresh), now
        for marker in failure_markers:
            if marker in fresh:
                return f"failed:{marker}", fresh
        if ready_marker in fresh or healthy(host):
            return "ready", fresh
        if progress is not None:
            line = latest_progress(fresh)
            if line and _progress_key(line) != reported:
                reported = _progress_key(line)
                try:
                    progress(line)
                except Exception:  # noqa: BLE001 - a display failure never ends a start
                    pass
        if getattr(proc, "poll", lambda: None)() is not None:
            return "exited", fresh
        if now - last_growth >= stall_s:
            return "stalled", fresh
        if cancel is not None:
            cancel.wait(interval)
        else:
            time.sleep(interval)


def _kill(proc) -> None:
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    try:
        ttyguard.run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


#: Bounded wait for the job to drain to zero active processes after terminate(). A job
#: close is asynchronous, so this poll is the finite proof window.
_JOB_DRAIN_TIMEOUT = 3.0
_JOB_DRAIN_INTERVAL = 0.1


def _poll_job_drained(job, timeout=None, interval=None) -> bool:
    """True iff active_process_count(job) reaches 0 within a finite deadline. A query
    failure (None) is decisive — cannot prove -> False (retain). A positive count keeps
    polling until the deadline; a final read decides."""
    timeout = _JOB_DRAIN_TIMEOUT if timeout is None else timeout
    interval = _JOB_DRAIN_INTERVAL if interval is None else interval
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        count = jobkill.active_process_count(job)
        if count is None:
            return False
        if count == 0:
            return True
        time.sleep(interval)
    return jobkill.active_process_count(job) == 0


def _close_log(owned) -> None:
    try:
        owned.log_file.close()
    except Exception:  # noqa: BLE001, S110 - a closed handle on a dead process is not news
        pass


_CLEANUP_LOCKS_GUARD = threading.Lock()


def _cleanup_lock_for(owned) -> threading.Lock:
    """A per-OwnedEngine cleanup lock, created once under a global guard (thread-safe) and
    cached on the instance. It serializes terminate_owned across ALL callers — atexit
    stop(), a direct terminate_owned, and the backend — so two cleanups can never
    terminate/close the SAME job concurrently (a handle-reuse hazard)."""
    with _CLEANUP_LOCKS_GUARD:
        lock = getattr(owned, "_cleanup_lock", None)
        if lock is None:
            lock = threading.Lock()
            try:
                owned._cleanup_lock = lock
            except Exception:  # noqa: BLE001 - a slotted/foreign object still gets a fresh lock
                pass
        return lock


def terminate_owned(owned):
    """Per-OwnedEngine-serialized terminal-shutdown proof. If a cleanup for THIS engine is
    already in flight, return retained (busy) rather than racing a second terminate/close on
    the same job — it never WAITS (no UI stall). See _terminate_owned_inner for the proof."""
    from .llm_backend import TerminalShutdown
    lock = _cleanup_lock_for(owned)
    if not lock.acquire(blocking=False):
        return TerminalShutdown(owned=True, attempted=False, main_exited=False, tree="unknown",
                                pid=getattr(owned.proc, "pid", None), retained=True,
                                error="cleanup_in_progress")
    try:
        return _terminate_owned_inner(owned)
    finally:
        lock.release()


def _terminate_owned_inner(owned):
    """The ONE terminal-shutdown proof for an owned engine — used by stop(), the
    start-failure path and the backend, so no caller invents a divergent proof.

    Job branch: terminate the job WITHOUT closing the handle, wait for the main process
    to exit, then poll the job to zero active processes; close ONLY on that proof
    (tree='confirmed'). No-job branch: _kill + wait (main-only, tree='unknown'). A
    CloseHandle success is a KILL REQUEST, not a completion — the drain poll is the
    proof, and the host is unregistered ONLY on a confirmed terminal state. Any
    failure / query-None / timeout / not-exited RETAINS the handle and the record for a
    retry (returns retained=True). Returns a TerminalShutdown; it does NOT touch any
    backend's self._owned (the caller owns that).
    """
    from .llm_backend import TerminalShutdown, wait_for_exit

    proc = owned.proc
    job = owned.job
    pid = getattr(proc, "pid", None)
    if job is None:
        error = None
        attempted = False
        try:
            if owned.alive:
                _kill(proc)
                attempted = True
            main_exited = wait_for_exit(proc)
        except Exception as exc:  # noqa: BLE001 - type-only diagnostic
            error = type(exc).__name__
            main_exited = False
        if not main_exited:
            return TerminalShutdown(owned=True, attempted=attempted, main_exited=False,
                                    tree="unknown", pid=pid, retained=True, error=error)
        if not unregister_owned(owned):         # bookkeeping failure retains, never loses the handle
            return TerminalShutdown(owned=True, attempted=attempted, main_exited=True,
                                    tree="unknown", pid=pid, retained=True, error="unregister_failed")
        _close_log(owned)
        return TerminalShutdown(owned=True, attempted=attempted, main_exited=True,
                                tree="unknown", pid=pid, retained=False, error=error)
    error = None
    try:
        if not jobkill.terminate(job):
            error = "terminate_failed"          # reported, but proof is independent
        main_exited = wait_for_exit(proc)
        drained = _poll_job_drained(job)
    except Exception as exc:  # noqa: BLE001 - type-only diagnostic
        error = type(exc).__name__
        main_exited = drained = False
    if not (main_exited and drained):
        return TerminalShutdown(owned=True, attempted=True, main_exited=main_exited,
                                tree="unknown", pid=pid, retained=True, error=error)
    try:
        closed = jobkill.close(job)             # can raise — a raw raise would lose the handle
    except Exception as exc:  # noqa: BLE001 - type-only; the proof stands, retain the handle
        return TerminalShutdown(owned=True, attempted=True, main_exited=True,
                                tree="confirmed", pid=pid, retained=True, error=type(exc).__name__)
    if not unregister_owned(owned):             # registry bookkeeping is SEPARATE from the proof
        return TerminalShutdown(owned=True, attempted=True, main_exited=True,
                                tree="confirmed", pid=pid, retained=True, error="unregister_failed")
    _close_log(owned)
    if not closed:
        # Tree proof stands independent of cleanup; do NOT discard the handle.
        return TerminalShutdown(owned=True, attempted=True, main_exited=True,
                                tree="confirmed", pid=pid, retained=True,
                                error=error or "close_failed")
    owned.job = None
    return TerminalShutdown(owned=True, attempted=True, main_exited=True,
                            tree="confirmed", pid=pid, retained=False, error=error)


def unregister_owned(owned) -> bool:
    """Ownership-checked registry removal under the cross-process RMW lock: drop the ninfer
    entry ONLY if owner=='litetui' AND baseUrl AND pid ALL match this owned engine — so a
    FOREIGN entry, or a REPLACEMENT that reused the same host/port (a different pid), is never
    removed by our cleanup. Read and write happen under one lock hold so no writer loses the
    other's update. Missing config -> nothing to remove (True). An I/O/parse failure or a
    contended lock -> False, so the caller retains rather than losing the handle over
    bookkeeping. Never a broad URL-only delete."""
    if not getattr(owned, "registered", True):
        return True                          # never registered -> nothing to remove
    return _remove_entry(owned.host, getattr(owned.proc, "pid", None))


def _remove_entry(host, pid) -> bool:
    """Drop the ninfer entry whose owner is litetui and whose baseUrl AND pid both match, under
    the registry lock. The one removal `unregister_owned` and `stop_adopted` share."""
    host = str(host).rstrip("/")
    path = _config_path()
    try:
        with _registry_lock():
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return True                  # nothing registered -> nothing to remove
            entries = body.get("extraEndpoints") or []
            body["extraEndpoints"] = [
                e for e in entries
                if not (isinstance(e, dict) and e.get("kind") == "ninfer"
                        and e.get("owner") == "litetui"
                        and str(e.get("baseUrl", "")).rstrip("/") == host and e.get("pid") == pid)
            ]
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
            os.replace(tmp, path)
            return True
    except (OSError, ValueError):            # incl. OwnershipError (contended) -> retain the handle
        return False
    return True


def _fail_start(owned, message, *, reason=None):
    """A start that failed but left a RESIDENT engine (weights load before the port binds).

    The retained OwnedEngine HANDLE is the real ownership — NOT a registry row. We do NOT
    register_host here: register_host filters by kind and would remove a FOREIGN ninfer
    entry (agreed: no foreign overwrite). atexit.register(stop) is registered BEFORE cleanup
    as best-effort recovery (NOT a force-kill guarantee — the job covers assigned members
    only). Then the unified cleanup is ATTEMPTED (never keep-running-only). The typed
    exception carries retained_owned when the cleanup was not confirmed OR when it raised
    unexpectedly — so an unresolved handle is ALWAYS surfaced to the caller, never lost."""
    atexit.register(stop, owned)
    try:
        result = terminate_owned(owned)
    except Exception:  # noqa: BLE001 - any unexpected cleanup failure must still surface the handle
        raise EngineStartFailed(message, retained_owned=owned, reason=reason)
    finally:
        # LiteTUI's OWN handle to the log must not leak on a failed start. The child keeps
        # writing through its own handle (independent on Windows) and _log_tail reads from
        # disk, so closing ours is safe whether we retain the engine for a retry or not.
        # (On the confirmed path _terminate_owned_inner already closed it; _close_log no-ops.)
        _close_log(owned)
    raise EngineStartFailed(message, retained_owned=(owned if result.retained else None), reason=reason)


def stop(owned: OwnedEngine) -> None:
    """Stop the engine we started and PROVE it terminated, via the unified terminate_owned.
    atexit-registered and best-effort; ignores the return and retains on an unconfirmed
    outcome for a later retry. (The job kill is atomic over the ASSIGNED tree; the taskkill
    walk is the no-job fallback — measured 3-43s live/dead, so it is a fallback, not a
    belt-and-braces second step.)

    LiteTUI's OWN handle to the log is closed in a finally, mirroring `_fail_start`. The
    terminal proof is retained on an unconfirmed outcome and `_terminate_owned_inner` only
    closes the log on a confirmed one, so without this the parent's FileIO leaks whenever
    the kill cannot be confirmed — including the atexit path. The child keeps writing
    through its own handle (independent on Windows) and `_log_tail` reads from disk, so
    closing ours is safe either way (a double close on the confirmed path is a no-op)."""
    try:
        terminate_owned(owned)
    finally:
        _close_log(owned)


