"""Owning a Basalt server from LiteTUI — the launcher `BasaltBackend` calls.

Basalt (https://github.com/jesdga95/basalt, MIT) is a Strata fork for NVIDIA Blackwell cards
(RTX 50, CUDA 13): Qwen3.8-Flash-Next on one GPU plus system RAM. Ryan, 2026-11-19: "makes
sure everything loads and acts exactly like any other backend and model in Litetui".

Its server is Strata's `serve/` grown apart: the same `/health` (with `loaded`), `/v1/models`
with a status, `POST /v1/load` and `/v1/unload`, OpenAI `/v1` + Anthropic `/v1/messages`. So
this is `strata_engine` with the differences below, and everything else is `strata_engine`'s
own code (`spawn_server`, `terminate_owned`, the port and GPU checks):

  * WHERE. `basalt_root` (setting), else LiteSuite's `basaltRoot`, else `~/.litesuite/llm/basalt`:
    a release archive or a build, i.e. `bin/basalt(.exe)`, `serve/`, `tools/`, `data/` and a
    `.venv` with its requirements.
  * A MODEL IS ONE FILE. There is no `strata-<model>.json`: each `*.basalt` under the install's
    `packs/` or `models/` folder, or in `basalt_packs_dir` (a second folder, e.g. another drive),
    is a prepared model, and THAT is the model list. `basalt_config` names the one to serve (the setting keeps Strata's name so `/model` is one door).
  * THE RUN CONFIG IS WRITTEN BY LITETUI. Basalt takes a version 2 JSON (`--config`); LiteTUI writes
    one beside its log from the settings (`run_config`), never into the install.
  * READY IS NOT "ANSWERING". The server prints its `ready` line and answers `/health` at once
    and loads the weights behind it. The wait keys on `weights  loaded` (server.py) or `/health`
    saying `loaded`, so a turn is never sent into a 45-second load.
  * WHO IT SAYS IT IS. `/health` says `"service": "basalt"` (`probe`).
  * RAM IS BASALT'S OWN GATE. It sizes its RAM tier from what is free and "refuses settings that
    would leave the machine without memory" (docs/running.md, memory floors), falling back to
    reading experts from the file. So only the card is checked here before a start.

🔴 MEMORY, measured 2026-11-19 on this box (96 GB, RTX 5090, ISTA IQ3_S pack of 81.6 GiB,
context 150,000, int8 KV, 2,000 MiB reserve): 26.4 GiB of weights on the card (30.1 GB used
in all), 36.9 GiB of experts pinned in RAM, loaded in about 40 s.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
from pathlib import Path

from . import ninfer_engine, strata_engine, ttyguard
from .llm_backend import BackendError
from .strata_engine import EngineStartFailed, OwnedEngine, terminate_owned  # noqa: F401 - the launcher API

__all__ = ["EngineStartFailed", "OwnedEngine", "start", "terminate_owned"]

BASALT_REPO_URL = "https://github.com/jesdga95/basalt"
#: `serve/server.py`'s line after every weights load (`tagged("weights", "loaded, ctx=...")`).
BASALT_READY_MARKER = "weights  loaded"
#: A load is about 40 s here; the first after a reboot reads 80 GiB from disk.
BASALT_START_TIMEOUT_S = 900
#: Basalt's own default is 8080, which Strata's installs and LiteSuite's Whisper also use.
BASALT_DEFAULT_PORT = 8091
#: The model's trained positions (docs/configuration: rope-scaling extends past it).
BASALT_TRAINED_CONTEXT = 262144
#: Basalt's own default is 8,192 tokens; an agent session needs more. Used when nothing is set.
BASALT_DEFAULT_CONTEXT = 131072
#: Where a model's own limits differ from the Flash-Next numbers above, by the architecture its
#: pack names (the `arch` of the pack's `meta` section). Inkling 2 Mini trains to 1,048,576 tokens
#: and its engine takes only `--kv fp16|fp8` (src/models/inkling2/adapter.cpp), so the `int8`
#: LiteTUI picks for a long context on the other models would stop it at start.
INKLING_ARCH = "inkling2"
ARCH_TRAINED_CONTEXT = {INKLING_ARCH: 1048576}
ARCH_KV_CHOICES = {INKLING_ARCH: ("fp16", "fp8")}
#: Folders of an install that hold packs.
PACK_DIRS = ("packs", "models")
#: The CUDA 13 runtime the engine links (docs/platforms.md: "on the library path").
RUNTIME_DLL = "cudart64_13.dll"


def basalt_root(settings) -> Path:
    """The install folder: the setting, else the one LiteSuite names, else beside its other engines."""
    chosen = str(getattr(settings, "basalt_root", "") or "").strip() or strata_engine.litesuite_folder("basaltRoot")
    return Path(chosen) if chosen else ninfer_engine.litesuite_llm_dir() / "basalt"


def engine_exe(root: Path) -> Path:
    return root / "bin" / ("basalt.exe" if os.name == "nt" else "basalt")


def server_python(root: Path) -> Path:
    return strata_engine.server_python(root)


def list_packs(settings) -> list[Path]:
    """Every `.basalt` file in the install's pack folders and in `basalt_packs_dir` (a second
    folder, such as one on another drive), sorted. THE MODEL LIST FOR THIS BACKEND."""
    root = basalt_root(settings)
    folders = [root / name for name in PACK_DIRS]
    extra = str(getattr(settings, "basalt_packs_dir", "") or "").strip()
    if extra:
        folders.append(Path(extra))
    found: set[Path] = set()
    for folder in folders:
        if folder.is_dir():
            found.update(p for p in folder.glob("*.basalt") if p.is_file())
    return sorted(found)


def basalt_pack(settings) -> Path | None:
    """The pack to serve: the setting, else the ONE the install holds (two and no choice is None)."""
    chosen = str(getattr(settings, "basalt_config", "") or "").strip()
    if chosen:
        return Path(chosen)
    found = list_packs(settings)
    return found[0] if len(found) == 1 else None


def read_config(path) -> dict | None:
    """What `/model` and `/engine status` ask of a listed model. A pack carries no run config
    (LiteTUI writes it), so this says whether `path` is a pack and the id it will be served under."""
    path = Path(path)
    if path.suffix.lower() != ".basalt" or not path.is_file():
        return None
    return {"pack": str(path), "model_name": model_name(path)}


def config_context(cfg: dict | None) -> int | None:
    """A pack has no context of its own; the context is LiteTUI's setting."""
    return None


#: The names `StrataBackend` and `/model` call on whichever launcher a backend has.
install_root = basalt_root
list_configs = list_packs
chosen_config = basalt_pack


def model_name(pack: Path) -> str:
    """The id the server is told to report (`alias`), so LiteTUI knows it before the server answers."""
    return pack.stem


def default_host(settings) -> str:
    explicit = str(getattr(settings, "basalt_host", "") or "").strip().rstrip("/")
    return explicit or f"http://127.0.0.1:{BASALT_DEFAULT_PORT}"


def context_for(settings) -> int:
    value = getattr(settings, "basalt_max_context", None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return BASALT_DEFAULT_CONTEXT


# ── the load options ────────────────────────────────────────────────────────
# Ryan, 2026-11-19 (T1355 choice 2): the options that matter for a load, plus a free-text field
# so nothing is unreachable; Basalt's whole catalog (about 60 settings) is a later card.
# Each is a `basalt_<name>` setting written into the model's entry of the run config under
# Basalt's own key (docs/configuration/index.md). Blank leaves Basalt's default, except where
# `run_config` says LiteTUI has its own.

KV_CHOICES = ("fp16", "int8", "q4_0", "k8v4")
VISION_CHOICES = ("off", "dynamic", "cpu")
#: The section a pack holds its image projector in, and the header that lists the sections.
VISION_SECTION = "vision/mmproj"
_PACK_MAGIC = b"BASALTMF"
_PACK_HEADER_BYTES = 64 << 10
_PACK_HEADER = struct.Struct("<8sIIQ40s")
_PACK_ENTRY = struct.Struct("<64sQQ32s16s")
#: Basalt refuses a smaller reserve on Windows (docs/running.md, memory floors).
VRAM_RESERVE_FLOOR_MIB = 256
#: What LiteTUI starts Strata with too (T1306-A): RAM for parked conversations, so a side call
#: does not cost the main conversation its cache. Basalt's own default is 0 (off).
DEFAULT_CONVERSATION_CACHE_MIB = 8192
#: The free-text field: a JSON object of further Basalt settings for the model's entry.
EXTRA_FIELD = "basalt_extra_settings"
#: Settings that change what the model writes (docs/precision: "lossy routing flags"). Ryan
#: (T1355 choice 4): no switches for these; reachable only through the free-text field, and a
#: start that has one on says so.
LOSSY_KEYS = ("route-tail-skip", "route-prior", "relaxed-drafting")
#: Keys the run config owns; the free-text field may not replace them.
_OWNED_KEYS = ("model", "alias")


def extra_settings(settings) -> dict:
    """The free-text field parsed: a JSON object, or ValueError naming the field."""
    raw = str(getattr(settings, EXTRA_FIELD, "") or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{EXTRA_FIELD}: not valid JSON ({exc})") from exc
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ValueError(f'{EXTRA_FIELD}: write a JSON object such as {{"prefill": "auto:16384"}}')
    owned = [k for k in value if k in _OWNED_KEYS]
    if owned:
        raise ValueError(f"{EXTRA_FIELD}: {', '.join(owned)} is set by /model, not here")
    return value


def pack_sections(pack) -> list[str]:
    """The section names a `.basalt` file lists in its header (Basalt's tools/model_file.py: a
    64-byte header, then one 128-byte entry per section, all inside the first 64 KiB). Empty for
    a file that is missing, unreadable or not a pack: this only ever chooses a default."""
    try:
        with open(pack, "rb") as fh:
            head = fh.read(_PACK_HEADER_BYTES)
        magic, _version, count, _size, _pad = _PACK_HEADER.unpack_from(head, 0)
        if magic != _PACK_MAGIC:
            return []
        names = []
        for i in range(count):
            raw = _PACK_ENTRY.unpack_from(head, _PACK_HEADER.size + i * _PACK_ENTRY.size)[0]
            names.append(raw.rstrip(b"\0").decode("utf-8", "replace"))
        return names
    except (OSError, struct.error):
        return []


_ARCH_CACHE: dict = {}
_META_LIMIT = 256 << 20


def pack_arch(pack) -> str | None:
    """The architecture a pack names (`arch` in its `meta` section), or None for a file that is
    missing, unreadable, not a pack or names none. Cached per file version: `meta` lists every
    tensor, so it is read once, and this only ever picks per-model limits."""
    try:
        st = os.stat(pack)
        key = (str(pack), st.st_mtime_ns, st.st_size)
        if key in _ARCH_CACHE:
            return _ARCH_CACHE[key]
        arch = None
        with open(pack, "rb") as fh:
            head = fh.read(_PACK_HEADER_BYTES)
            magic, _version, count, _size, _pad = _PACK_HEADER.unpack_from(head, 0)
            if magic == _PACK_MAGIC:
                for i in range(count):
                    raw, off, size, _digest, _x = _PACK_ENTRY.unpack_from(head, _PACK_HEADER.size + i * _PACK_ENTRY.size)
                    if raw.rstrip(b"\0") == b"meta" and 0 < size <= _META_LIMIT:
                        fh.seek(off)
                        found = json.loads(fh.read(size)).get("arch")
                        arch = found if isinstance(found, str) else None
                        break
        _ARCH_CACHE[key] = arch
        return arch
    except (OSError, struct.error, ValueError, AttributeError):
        return None


def has_inkling_pack(settings) -> bool:
    """Does Basalt hold an Inkling 2 Mini pack? Then Basalt is the Inkling engine and `/model`
    lists no other engine's Inkling files (Ryan, 2026-11-24: "i also dont want 50x inkling options
    when i do slash model anymore ... just the one")."""
    return any(pack_arch(p) == INKLING_ARCH for p in list_packs(settings))


def pack_has_vision(pack) -> bool:
    """Does the pack carry its image projector (the mmproj given to the converter)?"""
    return VISION_SECTION in pack_sections(pack)


def effective_vision(settings, pack) -> str | None:
    """The `vision` value a start uses. Ryan, 2026-11-20: "turn it on in the settings by default if
    the mmproj is present". So a chosen value always wins (an explicit off stays off), and blank is
    `dynamic` for a pack that carries its projector; for a pack without one nothing is set, which
    is Basalt's off."""
    chosen = getattr(settings, "basalt_vision", None)
    if chosen:
        return chosen
    return "dynamic" if pack is not None and pack_has_vision(pack) else None


def vision_blank_label(settings) -> str:
    """What the blank choice of `basalt_vision` resolves to for the pack chosen now, for Settings."""
    pack = basalt_pack(settings)
    if pack is None:
        return "Automatic: dynamic when the pack has an image projector, else off"
    if pack_has_vision(pack):
        return f"Automatic: dynamic ({pack.stem} has an image projector)"
    return f"Automatic: off ({pack.stem} has no image projector)"


def validate(settings) -> None:
    """ValueError naming the first load option Basalt would refuse, before anything is spawned."""
    kv = getattr(settings, "basalt_kv", None)
    if kv and kv not in KV_CHOICES:
        raise ValueError(f"basalt_kv: choose one of {', '.join(KV_CHOICES)}")
    vision = getattr(settings, "basalt_vision", None)
    if vision and vision not in VISION_CHOICES:
        raise ValueError(f"basalt_vision: choose one of {', '.join(VISION_CHOICES)}")
    reserve = getattr(settings, "basalt_vram_reserve_mib", None)
    if reserve is not None and reserve < VRAM_RESERVE_FLOOR_MIB:
        raise ValueError(f"basalt_vram_reserve_mib: Basalt refuses less than {VRAM_RESERVE_FLOOR_MIB} MiB on Windows")
    cache = str(getattr(settings, "basalt_expert_cache", "") or "").strip()
    if cache and cache != "auto" and not (cache.isdigit() and int(cache) > 0):
        raise ValueError("basalt_expert_cache: write auto or a whole number of expert slots")
    for name in ("basalt_conversation_cache_mib", "basalt_spec"):
        value = getattr(settings, name, None)
        if value is not None and value < 0:
            raise ValueError(f"{name}: must be zero or more")
    extra_settings(settings)


def lossy_settings(settings) -> list[str]:
    """The output-changing Basalt settings the free-text field turns on, for the start warning."""
    try:
        extra = extra_settings(settings)
    except ValueError:
        return []
    return [key for key in LOSSY_KEYS if extra.get(key) not in (None, False, 0, "")]


def run_config(settings, pack: Path, port: int) -> dict:
    """The version 2 config LiteTUI starts Basalt with (docs/configuration/index.md).

    One model, so one process holds one model as every engine backend here does. The server
    listens on loopback only and treats this machine's requests as remote for image sources
    (`image-remote-only`): it then makes no outbound fetch and reads no file path a request names.
    """
    try:
        validate(settings)
    except ValueError as exc:
        raise BackendError(f"{exc}; nothing was started.") from exc
    context = context_for(settings)
    arch = pack_arch(pack)
    trained = ARCH_TRAINED_CONTEXT.get(arch, BASALT_TRAINED_CONTEXT)
    if context > trained:
        raise BackendError(
            f"basalt_max_context {context} is past the model's trained {trained} tokens — "
            "that needs RoPE scaling, which LiteTUI does not set for Basalt; nothing was started.")
    model: dict = {"model": str(pack), "alias": model_name(pack), "max-context": context}
    kv = getattr(settings, "basalt_kv", None)
    kv_choices = ARCH_KV_CHOICES.get(arch)
    if kv and kv_choices and kv not in kv_choices:
        raise BackendError(
            f"basalt_kv {kv} is not an option for {arch}: its engine takes {' or '.join(kv_choices)} "
            "(Settings > Basalt > KV cache); nothing was started.")
    if kv:
        model["kv"] = kv
    elif context > 8192 and not kv_choices:
        # As strata_engine.with_context: past 8,192 tokens a 16-bit KV cache takes the VRAM the
        # expert cache needs. Basalt's default is fp16.
        model["kv"] = "int8"
    reserve = getattr(settings, "basalt_vram_reserve_mib", None)
    if reserve is not None:
        model["vram-reserve-mib"] = reserve
    cache = str(getattr(settings, "basalt_expert_cache", "") or "").strip()
    if cache:
        model["expert-cache"] = cache if cache == "auto" else int(cache)
    parked = getattr(settings, "basalt_conversation_cache_mib", None)
    model["conversation-cache-mib"] = DEFAULT_CONVERSATION_CACHE_MIB if parked is None else parked
    spec = getattr(settings, "basalt_spec", None)
    if spec is not None:
        model["spec"] = spec
    vision = effective_vision(settings, pack)
    if vision:
        model["vision"] = vision
    model.update(extra_settings(settings))
    return {"version": 2, "models": [model],
            "server": {"host": "127.0.0.1", "port": port, "image-remote-only": True}}


# ── the start gate ──────────────────────────────────────────────────────────

def engine_process_alive() -> bool:
    """Is any Basalt process running? True on doubt, so we never start a second."""
    try:
        import psutil
        return any(Path(p.info.get("name") or "").stem.lower() == "basalt"
                   for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001 - cannot ask -> assume alive, refuse to spawn
        return True


def probe(host: str, timeout: float = 2.0) -> tuple[str, object, str]:
    """`strata_engine.probe` for a Basalt server: ('basalt' | 'foreign' | 'silent', /health JSON, what it said).
    Basalt always names itself, so a server without `"service": "basalt"` is never taken for one."""
    status, body = strata_engine._get(f"{host}/health", timeout)
    if status is None:
        return "silent", None, "no answer to /health"
    if isinstance(body, dict) and body.get("service") == "basalt":
        return "basalt", body, ""
    said = (f"/health says service={body['service']!r}" if isinstance(body, dict) and "service" in body
            else f"/health: {status}, with no service name")
    return "foreign", body, said


def foreign_clause(host: str, label: str = "Basalt") -> str | None:
    """`strata_engine.foreign_clause` with Basalt's own probe: who holds the port when it is not a Basalt server."""
    kind, _body, said = probe(host)
    if kind == "basalt":
        return None
    holder = strata_engine.port_holder(host)
    if kind == "silent" and not holder.found:
        return None
    port = host.rsplit(":", 1)[-1]
    who = holder.describe() if holder.found else "another server (its owner could not be found)"
    verdict = (f"which is not a {label} server ({said})" if kind == "foreign"
               else f"which does not answer HTTP like a {label} server ({said})")
    return f"port {port} is held by {who}, {verdict}"


def refuse_reason(settings, cfg: dict, host: str, *, healthy) -> str | None:
    """Why a start must NOT happen, or None (the user 2026-09-10: check, ask, then load)."""
    if healthy(host):
        return f"a Basalt server is already answering at {host} — attach to it; nothing was started."
    if probe(host)[0] == "basalt":
        # Up with its model unloaded or still loading: a second server could not bind the port.
        return (f"a Basalt server is already answering at {host} without its model loaded — load it from "
                "that server's own page, or stop it where it was started; nothing was started.")
    if engine_process_alive():
        return ("a Basalt process is already running (still loading, or started elsewhere) — "
                "wait for it or stop it where it was started; nothing was started.")
    clause = foreign_clause(host)
    if clause:
        return f"{clause} — stop it or change basalt_host in /settings; nothing was started."
    # The card only: Basalt sizes its own RAM tier and refuses what would not fit (module note).
    return strata_engine.gpu_refusal(strata_engine.gpu_memory_mib())


# ── the process we own ──────────────────────────────────────────────────────

def log_path() -> Path:
    from .paths import data_root
    return data_root() / ".basalt" / "litetui-basalt-server.log"


def runtime_missing(root: Path) -> str | None:
    """The sentence for a missing CUDA runtime, or None. Checked before the spawn because the
    engine otherwise dies in a Windows dialog, or with a bare exit code nobody can read."""
    if os.name != "nt":
        return None
    if (engine_exe(root).parent / RUNTIME_DLL).is_file() or shutil.which(RUNTIME_DLL):
        return None
    return (f"the CUDA 13 runtime ({RUNTIME_DLL}, cublas64_13.dll, cublasLt64_13.dll) is neither beside "
            f"{engine_exe(root)} nor on PATH — install the CUDA 13 runtime or copy those files there; "
            "nothing was started.")


def launch_plan(settings) -> tuple[list[str], Path, dict, str]:
    """(argv, cwd, the config that will run, host) for the next start, or BackendError naming
    what is missing. Pure apart from reading the install; spawns nothing."""
    root = basalt_root(settings)
    python, exe = server_python(root), engine_exe(root)
    if not python.is_file() or not exe.is_file() or not (root / "serve" / "server.py").is_file():
        raise BackendError(
            f"Basalt is not installed at {root} — unpack a release there or build it "
            f"({BASALT_REPO_URL}), make its .venv from requirements.txt, then set basalt_root to that folder.")
    missing = runtime_missing(root)
    if missing:
        raise BackendError(missing)
    pack = basalt_pack(settings)
    if pack is None or read_config(pack) is None:
        raise BackendError(
            "no Basalt model chosen — /model to pick one (each .basalt file in the install's packs "
            "folder or in basalt_packs_dir is a prepared model), or set basalt_config.")
    host = default_host(settings)
    port = host.rsplit(":", 1)[-1]
    if not port.isdigit():
        raise BackendError(f"basalt_host {host} names no port — use http://127.0.0.1:<port>.")
    run = run_config(settings, pack, int(port))
    run_path = log_path().with_name(f"{pack.stem}.litetui.json")
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(json.dumps(run, indent=1), encoding="utf-8")
    # What `basalt serve --config` runs (src/program/cli/serve.cpp), started directly so the
    # process LiteTUI owns is the server itself, as with Strata.
    argv = [str(python), "-m", "serve.server", "--engine-bin", str(exe), "--config", str(run_path)]
    return argv, root, {"model_name": model_name(pack), "pack": str(pack), "run": run}, host


def start(settings, *, healthy, spawn=ttyguard.popen, notice=None, progress=None,
          cancel=None) -> OwnedEngine:
    """Spawn Basalt's server for the chosen pack and wait for its weights (strata_engine's
    `spawn_server`, with this launcher's plan, gate, log, timeout and ready line)."""
    return strata_engine.spawn_server(settings, healthy=healthy, spawn=spawn, notice=notice, label="Basalt",
                                      plan=launch_plan, refuse=refuse_reason, log=log_path,
                                      timeout=BASALT_START_TIMEOUT_S, progress=progress, cancel=cancel,
                                      ready_marker=BASALT_READY_MARKER)
