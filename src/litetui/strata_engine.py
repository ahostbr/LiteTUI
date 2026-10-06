"""Owning a Strata server from LiteTUI — the launcher `StrataBackend` calls.

Strata (https://github.com/Niko1221/Strata, MIT) runs Qwen3.8-Flash-Next on one GPU
plus system RAM: every expert pinned in RAM, the dense weights, the KV cache and a
cache of the most-used experts in VRAM. One model per process, OpenAI-compatible.

WHAT AN INSTALL IS: Strata's own `setup.py` (LiteSuite's Model Hub runs it) leaves a
folder with `.venv`, `serve/server.py`, the engine, and one `strata-<model>.json` per
prepared model. That JSON is the whole launch recipe — the engine path, its args, the
port — so THE MODEL LIST FOR THIS BACKEND IS THE CONFIG FILES, and starting one is
`<root>/.venv python serve/server.py --engine strata --config <json> --port N`, the
same line Strata's `run-<model>.bat` writes.

REUSE, DO NOT REBUILD: the owned-process handle, the KILL_ON_JOB_CLOSE job and the
terminal-shutdown proof are `ninfer_engine`'s, used as they are. What is here is only
what differs — where the recipe comes from, what "ready" looks like, and the RAM gate.

🔴 MEMORY, measured 2026-10-06 on a 64 GB / RTX 5090 box with the IQ3_XXS files:
39.97 GiB of experts pinned (0.91 of the first shard's 47.0 GB), engine working set
42.6 GB, commit charge +74 GB (Windows also charges the ~31 GiB the engine takes in
VRAM), leaving 3.7 GB of RAM and 4.4 GB of commit. It fits only with the big
co-tenants closed, so `refuse_reason` checks RAM and commit BEFORE the spawn: a start
that cannot fit pages the whole machine for minutes before it fails.

# ponytail: the gate is that one measurement turned into two ratios. Tune
# STRATA_PINNED_FRACTION / STRATA_VRAM_COMMIT_FRACTION when a second box reports.
"""
from __future__ import annotations

import atexit
import ctypes
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from . import jobkill, ninfer_engine, ttyguard
from .llm_backend import BackendError, _arg_value
from .ninfer_engine import EngineStartFailed, OwnedEngine

__all__ = ["EngineStartFailed", "OwnedEngine", "start", "terminate_owned"]

STRATA_REPO_URL = "https://github.com/Niko1221/Strata"
#: `server.py`'s own line once the engine has loaded and the port is bound.
STRATA_READY_MARKER = "ready: http://"
#: A cold load is ~80 s here; the first one after a reboot can take minutes.
STRATA_START_TIMEOUT_S = 900
STRATA_DEFAULT_PORT = 8080
#: The model's trained positions. Past it Strata's setup adds RoPE scaling; LiteTUI does not.
STRATA_TRAINED_CONTEXT = 262144
#: Experts pinned in RAM as a share of the `--native` shard's size (see module note).
STRATA_PINNED_FRACTION = 0.91
#: Share of the free VRAM the engine takes that Windows charges to commit (see module note).
STRATA_VRAM_COMMIT_FRACTION = 0.9
#: Below this share of the card free, something else holds it (another model). Refuse.
STRATA_MIN_FREE_VRAM_FRACTION = 0.6


def _litesuite_strata_root() -> str:
    """`strataRoot` from LiteSuite's config.json — the folder its Model Hub installed into
    or was pointed at — so both apps use one install. "" when unset or unreadable."""
    try:
        body = json.loads(ninfer_engine._config_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no file or bad JSON means "LiteSuite chose nothing"
        return ""
    val = body.get("strataRoot") if isinstance(body, dict) else None
    return val.strip() if isinstance(val, str) else ""


def strata_root(settings) -> Path:
    """The install folder: the setting, else the one LiteSuite's Model Hub names, else
    where that hub installs by default."""
    chosen = str(getattr(settings, "strata_root", "") or "").strip() or _litesuite_strata_root()
    return Path(chosen) if chosen else ninfer_engine.litesuite_llm_dir() / "strata"


def server_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def read_config(path: Path) -> dict | None:
    """A `strata-<model>.json`, or None when it is not one (utf-8-sig: setup writes a BOM-less
    file, a hand edit in Notepad may not)."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("args"), list) or not cfg.get("exe"):
        return None
    return cfg


def list_strata_configs(settings) -> list[Path]:
    """Every prepared model in the install, sorted. THE MODEL LIST FOR THIS BACKEND."""
    root = strata_root(settings)
    if not root.is_dir():
        return []
    return [p for p in sorted(root.glob("strata-*.json")) if read_config(p) is not None]


def strata_config(settings) -> Path | None:
    """The config to serve: the setting, else the ONE the install holds. Two and no choice
    is None, not a guess — the same rule as `ninfer_engine.ninfer_artifact`."""
    chosen = str(getattr(settings, "strata_config", "") or "").strip()
    if chosen:
        return Path(chosen)
    found = list_strata_configs(settings)
    return found[0] if len(found) == 1 else None


def config_context(cfg: dict | None) -> int | None:
    val = _arg_value(list((cfg or {}).get("args") or ()), "--max-context")
    return int(val) if isinstance(val, str) and val.isdigit() else None


def default_host(settings) -> str:
    """Where a Strata server is expected: the explicit setting, else the chosen config's
    port on loopback. The port is a SETTING of the install (8080), not allocated per start,
    so there is no registry to read."""
    explicit = str(getattr(settings, "strata_host", "") or "").strip().rstrip("/")
    if explicit:
        return explicit
    path = strata_config(settings)
    cfg = read_config(path) if path else None
    port = cfg.get("port") if cfg else None
    return f"http://127.0.0.1:{port if isinstance(port, int) and port > 0 else STRATA_DEFAULT_PORT}"


def with_context(cfg: dict, max_context: int | None) -> dict:
    """`cfg` with its `--max-context` replaced, or `cfg` itself when nothing changes.

    Mirrors what Strata's setup writes for a context: above 8,192 tokens the KV cache is
    8-bit (`--kv int8`, setup.py's default) — without it a long context is stored at 16
    bits and takes twice the VRAM from the expert cache.
    """
    if not isinstance(max_context, int) or isinstance(max_context, bool) or max_context <= 0:
        return cfg
    if max_context == config_context(cfg):
        return cfg
    if max_context > STRATA_TRAINED_CONTEXT:
        raise BackendError(
            f"strata_max_context {max_context} is past the model's trained {STRATA_TRAINED_CONTEXT} "
            "tokens — that needs RoPE scaling, which Strata's own setup configures "
            "(run its setup with --context); nothing was started.")
    args = [str(a) for a in cfg["args"]]
    if "--max-context" in args[:-1]:
        args[args.index("--max-context") + 1] = str(max_context)
    else:
        args += ["--max-context", str(max_context)]
    if max_context > 8192 and "--kv" not in args:
        args += ["--kv", "int8"]
    return {**cfg, "args": args}


# ── the memory gate ─────────────────────────────────────────────────────────

class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                *[(name, ctypes.c_ulonglong) for name in (
                    "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                    "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]]


def memory_free_bytes() -> tuple[int | None, int | None]:
    """(available RAM, commit headroom) in bytes; None where it cannot be asked.

    Commit headroom is Windows-only (`ullAvailPageFile`): the limit every process on the
    box shares, and the one a Strata start came within 4.4 GB of."""
    if os.name == "nt":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys), int(status.ullAvailPageFile)
        except (AttributeError, OSError):
            pass
        return None, None
    try:
        import psutil
        return int(psutil.virtual_memory().available), None
    except Exception:  # noqa: BLE001 - cannot ask -> unknown, do not refuse on it
        return None, None


def gpu_memory_mib() -> tuple[int, int] | None:
    """(free, total) MiB of the first GPU from nvidia-smi, or None when it cannot be asked."""
    try:
        out = ttyguard.run(["nvidia-smi", "--query-gpu=memory.free,memory.total",
                            "--format=csv,noheader,nounits"], timeout=10)
        free, total = str(getattr(out, "stdout", out)).strip().splitlines()[0].split(",")
        return int(free), int(total)
    except Exception:  # noqa: BLE001 - absent tool is "unknown", not "refuse"
        return None


def engine_process_alive() -> bool:
    """Is any Strata engine process running? True on doubt, so we never start a second."""
    try:
        import psutil
        return any((p.info.get("name") or "").lower() in ("strata.exe", "strata")
                   for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001 - cannot ask -> assume alive, refuse to spawn
        return True


def _gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def native_bytes(cfg: dict) -> int:
    """Size on disk of the config's `--native` shard; 0 when it is not named or not readable.

    Its own function so a test states a size instead of creating a file of that size."""
    native = _arg_value([str(a) for a in cfg.get("args") or ()], "--native")
    try:
        return Path(native).stat().st_size if native else 0
    except OSError:
        return 0


def memory_refusal(cfg: dict, *, ram_free: int | None, commit_free: int | None,
                   gpu: tuple[int, int] | None) -> str | None:
    """Why this config cannot be loaded into the memory that is free, or None. Pure."""
    if gpu is not None and gpu[1] > 0 and gpu[0] < gpu[1] * STRATA_MIN_FREE_VRAM_FRACTION:
        return (f"only {gpu[0]} of {gpu[1]} MiB is free on the GPU — something else holds the card "
                "(another model?). Unload it first; nothing was started.")
    args = [str(a) for a in cfg.get("args") or ()]
    if "--mmap-experts" in args or "--resident-budget-gib" in args:
        return None          # Strata's low-RAM modes read experts from disk; no pin to size
    pinned = int(native_bytes(cfg) * STRATA_PINNED_FRACTION)
    if not pinned:
        return None
    if ram_free is not None and ram_free < pinned:
        return (f"Strata pins about {_gb(pinned)} of this model in RAM and only {_gb(ram_free)} is "
                "available — close other programs first; nothing was started.")
    if commit_free is not None:
        vram = (gpu[0] * 1024 * 1024 * STRATA_VRAM_COMMIT_FRACTION) if gpu else 0
        if commit_free < pinned + vram:
            return (f"loading this model needs about {_gb(pinned + vram)} of commit charge (RAM plus "
                    f"the VRAM Windows also charges) and only {_gb(commit_free)} is left before the "
                    "system limit — close other programs or enlarge the page file; nothing was started.")
    return None


def refuse_reason(settings, cfg: dict, host: str, *, healthy) -> str | None:
    """Why a start must NOT happen, or None (the user 2026-09-10: check, ask, then load)."""
    if healthy(host):
        return f"a Strata server is already answering at {host} — attach to it; nothing was started."
    if engine_process_alive():
        return ("a Strata engine process is already running (still loading, or started elsewhere) — "
                "wait for it or stop it where it was started; nothing was started.")
    ram_free, commit_free = memory_free_bytes()
    return memory_refusal(cfg, ram_free=ram_free, commit_free=commit_free, gpu=gpu_memory_mib())


# ── the process we own ──────────────────────────────────────────────────────

def log_path() -> Path:
    from .paths import data_root
    return data_root() / ".strata" / "litetui-strata-server.log"


def _log_event(text: str) -> None:
    """One timestamped line for outcomes that never spawn (a refusal must leave a trace: T877)."""
    try:
        lp = log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"--- litetui {time.strftime('%F %T')} | {text}\n")
    except OSError:
        pass


def _refuse(message: str) -> None:
    _log_event(f"REFUSED: {message}")
    raise BackendError(message)


def launch_plan(settings) -> tuple[list[str], Path, dict, str]:
    """(argv, cwd, the config that will run, host) for the next start, or BackendError
    naming what is missing. Pure apart from reading the install; spawns nothing."""
    root = strata_root(settings)
    python = server_python(root)
    server = root / "serve" / "server.py"
    if not python.is_file() or not server.is_file():
        raise BackendError(
            f"Strata is not installed at {root} — install it from LiteSuite's Model Hub (Strata), "
            f"or set strata_root to a folder Strata's setup prepared ({STRATA_REPO_URL}).")
    path = strata_config(settings)
    cfg = read_config(path) if path else None
    if cfg is None:
        raise BackendError(
            "no Strata model chosen — /model to pick one (each strata-<model>.json in the install "
            "is a prepared model), or set strata_config.")
    run_cfg = with_context(cfg, getattr(settings, "strata_max_context", None))
    run_path = path
    if run_cfg is not cfg:
        # A context LiteTUI chose: written beside the log, never over the install's own file.
        run_path = log_path().with_name(f"{path.stem}.litetui.json")
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(run_cfg, indent=1), encoding="utf-8")
    host = default_host(settings)
    port = host.rsplit(":", 1)[-1]
    if not port.isdigit():
        raise BackendError(f"strata_host {host} names no port — use http://127.0.0.1:<port>.")
    argv = [str(python), str(server), "--engine", "strata", "--config", str(run_path), "--port", port]
    return argv, root, run_cfg, host


def _last_words(fresh: str, limit: int = 400) -> str:
    """The END of what this spawn printed. `ninfer_engine._log_tail` keeps the first 400
    characters of the last five lines, and here those begin with the spawn header — a long
    install path pushed the engine's own reason off the end."""
    return " ".join(fresh.strip().splitlines()[-3:])[-limit:] or "(it printed nothing)"


def start(settings, *, healthy, spawn=ttyguard.popen, notice=None) -> OwnedEngine:
    """Spawn Strata's server for the chosen config and wait for it to load.

    `healthy(host) -> bool` and `spawn` are injected so the arms never load a model.
    `notice()` is called once, immediately before the spawn and never on a refusal (T865).
    """
    _log_event("engine start requested")
    try:
        argv, root, cfg, host = launch_plan(settings)
    except BackendError as exc:
        _refuse(str(exc))
    reason = refuse_reason(settings, cfg, host, healthy=healthy)
    if reason:
        _refuse(reason)
    model_id = str(cfg.get("model_name") or "strata")
    lp = log_path()
    lp.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(lp, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - the process writes here for its whole life
    log_file.write(f"\n--- litetui spawn {time.strftime('%F %T')} ---\n{' '.join(argv)}\n")
    log_file.flush()
    start_off = lp.stat().st_size      # the log is appended across spawns: only this spawn's bytes count
    if notice is not None:
        notice()
    proc = spawn(
        argv, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, cwd=str(root),
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )
    # Adopt it into a KILL_ON_JOB_CLOSE job before the wait, so ~40 GB of pinned RAM dies
    # with LiteTUI however LiteTUI dies (ninfer_engine.start carries the incident).
    job = jobkill.create()
    pid = getattr(proc, "pid", None)
    if job is not None and isinstance(pid, int) and not jobkill.assign(job, pid):
        _log_event(f"job assign FAILED for pid {pid} — orphan protection is off for this engine")
        jobkill.close(job)
        job = None
    owned = OwnedEngine(proc=proc, host=host, log_path=lp, log_file=log_file, model_id=model_id,
                        job=job, args=tuple(argv), registered=False)
    deadline = time.monotonic() + STRATA_START_TIMEOUT_S
    fresh = ""
    while time.monotonic() < deadline:
        try:
            with open(lp, "rb") as f:
                f.seek(start_off)
                fresh = f.read().decode("utf-8", "replace")
        except OSError:
            fresh = ""
        if STRATA_READY_MARKER in fresh or healthy(host):
            atexit.register(ninfer_engine.stop, owned)
            return owned
        if getattr(proc, "poll", lambda: None)() is not None:
            ninfer_engine._fail_start(owned, f"Strata exited before it was ready — {_last_words(fresh)}")
        time.sleep(1.0)
    ninfer_engine._fail_start(
        owned, f"Strata did not become ready in {STRATA_START_TIMEOUT_S}s — {_last_words(fresh)}")


def request_unload(host: str, timeout: float = 30.0) -> bool:
    """Ask the server to end its engine cleanly (`POST /v1/unload`), so the engine frees its
    pinned RAM itself instead of being killed holding it. Best-effort: False on any failure."""
    try:
        req = urllib.request.Request(f"{host}/v1/unload", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "LiteTUI"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001 - busy (409), gone or slow: the kill below still runs
        return False


def terminate_owned(owned):
    """Unload, then the shared terminal-shutdown proof (`ninfer_engine.terminate_owned`)."""
    if owned.alive:
        request_unload(owned.host)
    return ninfer_engine.terminate_owned(owned)
