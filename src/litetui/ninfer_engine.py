"""Owning `ninfer-serve` from LiteTUI — the launcher the backend calls.

🔴 RYAN, 2026-09-17 (liteask a-35456da0): *"LiteTUI may start it"* — for 5090 end
users running LiteTUI WITHOUT LiteSuite. Until this module the backend could
only ATTACH (`ninfer_backend.py` docstring), which made LiteTUI-alone dead
without LiteSuite. Attaching is still the default; starting is `/engine start`.

REUSE, DO NOT REBUILD (Ryan, same day: *"agents ... rebuild every system for
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
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import jobkill, ttyguard
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

    def __init__(self, message: str, *, retained_owned=None) -> None:
        super().__init__(message)
        self.retained_owned = retained_owned

NINFER_V3_MAGIC = b"NINFER\x00\x03"
NINFER_V3_HEADER_BYTES = 32
NINFER_MAX_DIRECTORY_BYTES = 64 * 1024 * 1024

#: LiteSuite's defaults (ninfer-args.ts). A user's chosen context wins.
NINFER_DEFAULT_MAX_CONTEXT = 32768
NINFER_DEFAULT_SPEC = "mtp"
NINFER_DEFAULT_DRAFT_TOKENS = 3
NINFER_DRAFT_TOKEN_RANGE = {"mtp": (1, 3), "dflash": (1, 7), "dflash2": (1, 7)}
NINFER_DEFAULT_KV_DTYPE = "fp8"

#: The engine's own words (ninfer-args.ts:184/195). A failure line ends the wait
#: NOW rather than at the timeout.
NINFER_READY_MARKER = "listening on http://"
NINFER_FAILURE_MARKERS = ("cannot bind", "server listen failed", "server failed during startup")
#: weights 19.7 GiB took 8.3 s on this box (log 11:59:50); a cold disk is slower.
NINFER_START_TIMEOUT_S = 180
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


def list_ninfer_artifacts() -> list[Path]:
    """Every `.ninfer` on disk, sorted. THE MODEL LIST FOR THIS BACKEND.

    🔴 ON NINFER THE MODEL IS NOT A CHOICE THE SERVER OFFERS. There is one
    artifact per process, fixed when the engine starts, so `/model`'s usual
    question — "which of the models this server has loaded?" — has no meaning
    here. The real question is which FILE the next engine will serve, and until
    T860 nothing in the TUI could ask it: `/model` and `/load` both list server
    models, and the only way to choose was typing a path into /settings.

    Extracted so the picker and `ninfer_artifact` below cannot disagree about
    where artifacts live.
    """
    root = artifacts_dir()
    return sorted(root.glob("*.ninfer")) if root.is_dir() else []


def ninfer_artifact(settings) -> Path | None:
    """The artifact to serve: the setting, else the ONE `.ninfer` LiteSuite pulled.

    Two candidates and no choice is None, not a guess — the wrong 22 GB file is
    a long wait that ends in the wrong model.

    ⚠️ AND THAT GUARD TOOK RYAN'S START PATH AWAY, SILENTLY, ON 2026-09-17.
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
    preserve_thinking: bool = True,
    vision: bool = True,
) -> list[str]:
    """LiteSuite's buildNInferArgs, flag for flag. Pure, so it has an arm."""
    args = [str(artifact), "--host", host, "--port", str(port), "--model-id", model_id]
    ctx = max_context if isinstance(max_context, int) and max_context > 0 else NINFER_DEFAULT_MAX_CONTEXT
    args += ["--max-context", str(ctx)]
    # Passed only when chosen (None = the engine's own default of 1), clamped into
    # NINFER_CONCURRENCY_RANGE. `bool` is an int; it is not a lane count.
    if isinstance(max_concurrency, int) and not isinstance(max_concurrency, bool) and max_concurrency > 0:
        lo, hi = NINFER_CONCURRENCY_RANGE
        args += ["--max-concurrency", str(min(hi, max(lo, max_concurrency)))]
    want = spec or NINFER_DEFAULT_SPEC
    # 🔴 gated on the ARTIFACT, not on the flag parser: the failure otherwise
    # lands at model load, after the user has waited (ninfer-args.ts).
    if want != "none" and want in components:
        lo, hi = NINFER_DRAFT_TOKEN_RANGE.get(want, (1, 3))
        chosen = draft_tokens if isinstance(draft_tokens, int) and draft_tokens > 0 else NINFER_DEFAULT_DRAFT_TOKENS
        args += ["--spec", want, "--draft-tokens", str(min(hi, max(lo, chosen))), "--lm-head-draft"]
    args += ["--kv-dtype", kv_dtype]
    if preserve_thinking:
        args.append("--preserve-thinking")
    # 15:0x 2026-09-17, Ryan's screenshot: the 35B-A3B carries a `vision` component, the
    # engine was started without --vision, and the first view_image put a media part in the
    # history -> every request after it was `HTTP 400 vision disabled` (serving.md:43-45).
    # Gated on the artifact like --spec: the flag on a text-only artifact fails at load.
    if vision and "vision" in components:
        args.append("--vision")
    return args


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

    RYAN, 2026-09-18: *"it spawned that ninfer server then refused to close it
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


def stop_registered(entry: dict) -> bool:
    """FAIL CLOSED (option A). A registered engine we hold no live handle for is
    identified only by {owner, kind, pid}. Under PID reuse a pid-only `taskkill /PID`
    can terminate an UNRELATED process, and without a retained handle or a verified
    process identity (e.g. creation time) we cannot confirm the pid is still OUR engine.

    So this does NOT kill and does NOT drop the record: it returns False (nothing
    stopped), preserving the entry for a future identity-verified stop. The old
    behaviour — taskkill by unverified pid, then unregister and return True even when
    taskkill raised — could kill a reused pid and permanently erase the only ownership
    record while the real process survived. Validated so a malformed/foreign entry is a
    clean no-op either way.
    """
    pid = entry.get("pid")
    if entry.get("owner") != "litetui" or entry.get("kind") != "ninfer":
        return False
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    return False  # verified identity is a separate slice; until then, no pid-only kill


def register_host(base_url: str, *, pid: int | None = None) -> bool:
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
    #: `atexit` ever runs. That is the case Ryan hit on 2026-09-18 —
    #: *"killing litetui didnt close the server"* — leaving ninfer-serve.exe
    #: pid 269276 resident with 10.7 GB. None on non-Windows or if the job
    #: could not be made; `stop` then falls back to the taskkill walk.
    job: int | None = None
    #: The argv it was spawned with. `/engine status` and `seat_snapshot` read the
    #: running capacity from HERE for an owned engine — the flags are startup-only
    #: and no route reports them (ninfer http_server.cpp:480-497 passes id, time and
    #: max_context only), so the setting is not the answer; the argv is.
    args: tuple[str, ...] = ()

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
    as far as launching a process. Measured 2026-09-18 00:2x: Ryan ran `/model`
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


def refuse_reason(settings, *, healthy) -> str | None:
    """Why a start must NOT happen, or None. Every path that puts a model in VRAM
    answers here first (Ryan 2026-09-10: check, ask, then load)."""
    explicit = str(getattr(settings, "ninfer_host", "") or "").strip().rstrip("/")
    if explicit and healthy(explicit):
        return f"an NInfer engine is already answering at {explicit} (ninfer_host) — attach to it; nothing was started."
    reg = registered_host()
    if reg is not None:
        if healthy(reg):
            return f"an NInfer engine is already registered and answering at {reg} — attach to it; nothing was started."
        if engine_process_alive():
            # A process exists and is not answering yet: it is LOADING (weights take
            # ~8 s, the port binds after). Starting a second one now is the VRAM bug.
            return (f"an NInfer engine is registered at {reg} and still loading (a ninfer-serve process "
                    "exists) — wait for it, then attach; nothing was started.")
        # Registered, silent, and NO process: a stale entry. Measured 2026-09-17 13:2x — a
        # holder killed with TerminateProcess never ran its atexit, so the dead port stayed
        # in LiteSuite's config and would have refused every start forever.
        unregister_host(reg)
    free = gpu_free_mib()
    if free is not None and free < NINFER_MIN_FREE_MIB:
        return (f"only {free} MiB free on the GPU; the engine needs about {NINFER_MIN_FREE_MIB} MiB "
                "(weights + runtime + 32k fp8 KV). Something else holds the card — nothing was started.")
    return None


def start(settings, *, healthy, spawn=ttyguard.popen, notice=None) -> OwnedEngine:
    """Spawn ninfer-serve with LiteSuite's argv, wait for its ready line, register it.

    `healthy(host) -> bool` and `spawn` are injected so the arms never touch the
    real card. Raises BackendError with the reason on every refusal or failure.

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
    exe = ninfer_executable(settings)
    if not exe.is_file():
        msg = f"ninfer-serve not installed at {exe} — install it from LiteSuite's Model Hub, or set ninfer_executable."
        _log_event(f"REFUSED: {msg}")
        raise BackendError(msg)
    artifact = ninfer_artifact(settings)
    if artifact is None or not artifact.is_file():
        msg = "no NInfer artifact chosen — set ninfer_artifact to a .ninfer file (LiteSuite's Model Hub pulls one)."
        _log_event(f"REFUSED: {msg} (ninfer_artifact={getattr(settings, 'ninfer_artifact', None)!r})")
        raise BackendError(msg)
    directory = read_artifact_directory(artifact)
    # 14:2x 2026-09-17: the 35B-A3B header carries no model_id; the pinned fallback made the
    # running MoE advertise itself as qwen3.8-27b in every picker. The file's own name is
    # the honest label when the header says nothing.
    model_id = artifact_model_id(directory, fallback=artifact.stem)
    port = free_port()
    host = f"http://127.0.0.1:{port}"
    ctx = getattr(settings, "ninfer_max_context", None)
    conc = getattr(settings, "ninfer_max_concurrency", None)
    args = build_ninfer_args(
        artifact, port, model_id,
        max_context=ctx if isinstance(ctx, int) else None,
        max_concurrency=conc if isinstance(conc, int) else None,
        components=artifact_components(directory),
    )
    lp = log_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(lp, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - the process writes here for its whole life
    log_file.write(f"\n--- litetui spawn {time.strftime('%F %T')} ---\n{exe} {' '.join(args)}\n")
    log_file.flush()
    # 14:1x 2026-09-17: the log is APPENDED across spawns, so a marker from an earlier run
    # must not count — only bytes written after this spawn's header are this engine's.
    start_off = lp.stat().st_size
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
    # is force-killed. Ryan, 2026-09-18: *"killing litetui didnt close the
    # server"* — ninfer-serve.exe pid 269276, 10.7 GB, parent gone. A job needs
    # nobody to run anything: the kernel kills every member when the last
    # handle closes, and Windows closes our handles for us when we die however
    # we die. Descendants inherit the job, so there is nothing to enumerate.
    #
    # `jobkill` already existed for `ttyguard` and `lifecycle_hooks`; this
    # spawn simply never used it.
    job = jobkill.create()
    pid = getattr(proc, "pid", None)
    if job is not None and isinstance(pid, int):
        if not jobkill.assign(job, pid):
            # Not fatal: `stop` still has the taskkill path. But say so, or a
            # later orphan looks like the job silently failing to hold.
            _log_event(f"job assign FAILED for pid {pid} — orphan protection is off for this engine")
            jobkill.close(job)
            job = None
    deadline = time.monotonic() + NINFER_START_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with open(lp, "rb") as f:
                f.seek(start_off)
                fresh = f.read().decode("utf-8", "replace")
        except OSError:
            fresh = ""
        for marker in NINFER_FAILURE_MARKERS:
            if marker in fresh:
                failed = OwnedEngine(proc=proc, host=host, log_path=lp, log_file=log_file,
                                     model_id=model_id, job=job, args=tuple(args))
                _fail_start(failed, f"ninfer-serve failed to start: {marker} — {_log_tail(lp)}")
        if NINFER_READY_MARKER in fresh or healthy(host):
            owned = OwnedEngine(proc=proc, host=host, log_path=lp, log_file=log_file,
                                model_id=model_id, job=job, args=tuple(args))
            register_host(host, pid=pid)
            atexit.register(stop, owned)
            return owned
        if getattr(proc, "poll", lambda: None)() is not None:
            break
        time.sleep(0.5)
    failed = OwnedEngine(proc=proc, host=host, log_path=lp, log_file=log_file,
                         model_id=model_id, job=job, args=tuple(args))
    _fail_start(failed, f"ninfer-serve did not become ready in {NINFER_START_TIMEOUT_S}s — {_log_tail(lp)}")


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
    pid = getattr(owned.proc, "pid", None)
    host = str(owned.host).rstrip("/")
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


def _fail_start(owned, message):
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
        raise EngineStartFailed(message, retained_owned=owned)
    raise EngineStartFailed(message, retained_owned=(owned if result.retained else None))


def stop(owned: OwnedEngine) -> None:
    """Stop the engine we started and PROVE it terminated, via the unified terminate_owned.
    atexit-registered and best-effort; ignores the return and retains on an unconfirmed
    outcome for a later retry. (The job kill is atomic over the ASSIGNED tree; the taskkill
    walk is the no-job fallback — measured 3-43s live/dead, so it is a fallback, not a
    belt-and-braces second step.)"""
    terminate_owned(owned)


