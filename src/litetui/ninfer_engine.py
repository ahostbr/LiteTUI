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
import time
from dataclasses import dataclass
from pathlib import Path

from . import ttyguard
from .llm_backend import BackendError

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


def registered_host() -> str | None:
    """Same read as ninfer_backend.discover_ninfer_host, kept here so the launcher
    can refuse on it without importing the backend."""
    try:
        body = json.loads(_config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no file, no entry, bad JSON: all mean "no engine"
        return None
    for entry in body.get("extraEndpoints") or []:
        if isinstance(entry, dict) and entry.get("kind") == "ninfer":
            base = entry.get("baseUrl")
            if isinstance(base, str) and base.strip():
                return base.rstrip("/")
    return None


def register_host(base_url: str, *, pid: int | None = None) -> bool:
    """Add `{baseUrl, kind: ninfer, owner: "litetui", pid}` to LiteSuite's config; False when
    the file is not writable. `owner`/`pid` are the vocabulary NeonRack and NeonRelay agreed for
    the Model Hub (T819/T820, 2026-09-17): the hub labels an answering engine LiteSuite | LiteTUI
    | external by WHO WROTE THE ENTRY and may only Stop its own; an entry without `owner` reads
    as external. Every reader keys on baseUrl + kind only, so older readers are unaffected."""
    path = _config_path()
    try:
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
    path = _config_path()
    try:
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


def start(settings, *, healthy, spawn=ttyguard.popen) -> OwnedEngine:
    """Spawn ninfer-serve with LiteSuite's argv, wait for its ready line, register it.

    `healthy(host) -> bool` and `spawn` are injected so the arms never touch the
    real card. Raises BackendError with the reason on every refusal or failure.
    """
    reason = refuse_reason(settings, healthy=healthy)
    if reason:
        raise BackendError(reason)
    exe = ninfer_executable(settings)
    if not exe.is_file():
        raise BackendError(f"ninfer-serve not installed at {exe} — install it from LiteSuite's Model Hub, or set ninfer_executable.")
    artifact = ninfer_artifact(settings)
    if artifact is None or not artifact.is_file():
        raise BackendError("no NInfer artifact chosen — set ninfer_artifact to a .ninfer file (LiteSuite's Model Hub pulls one).")
    directory = read_artifact_directory(artifact)
    # 14:2x 2026-09-17: the 35B-A3B header carries no model_id; the pinned fallback made the
    # running MoE advertise itself as qwen3.8-27b in every picker. The file's own name is
    # the honest label when the header says nothing.
    model_id = artifact_model_id(directory, fallback=artifact.stem)
    port = free_port()
    host = f"http://127.0.0.1:{port}"
    ctx = getattr(settings, "ninfer_max_context", None)
    args = build_ninfer_args(
        artifact, port, model_id,
        max_context=ctx if isinstance(ctx, int) else None,
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
    proc = spawn(
        [str(exe), *args],
        stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT,
        cwd=str(exe.parent),  # the CUDA DLLs live beside the exe
    )
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
                _kill(proc)
                log_file.close()
                raise BackendError(f"ninfer-serve failed to start: {marker} — {_log_tail(lp)}")
        if NINFER_READY_MARKER in fresh or healthy(host):
            owned = OwnedEngine(proc=proc, host=host, log_path=lp, log_file=log_file, model_id=model_id)
            register_host(host, pid=getattr(proc, "pid", None))
            atexit.register(stop, owned)
            return owned
        if getattr(proc, "poll", lambda: None)() is not None:
            break
        time.sleep(0.5)
    _kill(proc)
    log_file.close()
    raise BackendError(f"ninfer-serve did not become ready in {NINFER_START_TIMEOUT_S}s — {_log_tail(lp)}")


def _kill(proc) -> None:
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    try:
        ttyguard.run(["taskkill", "/T", "/F", "/PID", str(pid)], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def stop(owned: OwnedEngine) -> None:
    """Stop ONLY the process we started, and take it out of the registry."""
    if owned.alive:
        _kill(owned.proc)
    unregister_host(owned.host)
    try:
        owned.log_file.close()
    except Exception:  # noqa: BLE001, S110 - a closed handle on a dead process is not news
        pass
