"""Owning a Xolotl server from LiteTUI — the launcher `XolotlBackend` calls.

Xolotl (https://github.com/Indras-Mirror/Xolotl, MIT) is a Strata fork that runs
DeepSeek-V4.1-Flash (552B MoE) on one GPU plus a pinned RAM arena plus NVMe. It serves
through Strata's own `serve/server.py`: the same `/health` (with `loaded`), `/v1/models`,
OpenAI `/v1` + Anthropic `/v1/messages`, `ready: http://` line and `POST /v1/unload`. So
this is `strata_engine` with four differences, and everything else is `strata_engine`'s
own code (`spawn_server`, `terminate_owned`, `read_config`, the memory readers):

  * WHERE. `xolotl_root` (setting), else LiteSuite's `xolotlRoot`, else
    `~/.litesuite/llm/xolotl`. Its models are the same `strata-<model>.json` files in that
    folder: Xolotl's setup.py names its configs so and its .gitignore keeps them out of git.
  * WHICH SERVER SCRIPT. A config whose `exe` is `ds41_generate` (DeepSeek-V4.1) runs
    `tools/v41/serve/serve_ds41.py`, `glm_generate` (GLM-5.3-Flash) `tools/glm/serve/
    serve_glm.py`, anything else (the Qwen configs) `serve/server.py`. The Python is
    `.venv-serve` (docs/WINDOWS.md), else setup.py's `.venv`, run as the docs run it, `-I`,
    plus `-u -X utf8` because `-I` ignores the PYTHONUNBUFFERED / PYTHONIOENCODING the spawn sets.
  * CONTEXT. Those two engines take `--ctx`, not `--max-context`.
  * THE MEMORY GATE. DeepSeek-V4.1 pins its expert arena (`--arena-gib`, engine default 60)
    and its conversation pool (`--conversation-cache-mib`, default 8192 MiB) in RAM, and the
    engine holds ~12.5 GiB more: Xolotl #14 measured 99.3 GB of private bytes at arena 72 +
    pool 8 GiB on a Windows RTX 5090 D. A Qwen config keeps Strata's own `--native` rule.

# ponytail: the 12.5 GiB is one box's measurement. Tune XOLOTL_OVERHEAD_GIB when Ryan's
# first real load reports its private bytes.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import ninfer_engine, strata_engine, ttyguard
from .llm_backend import BackendError, _arg_value
from .strata_engine import EngineStartFailed, OwnedEngine, read_config, terminate_owned  # noqa: F401 - the launcher API

__all__ = ["EngineStartFailed", "OwnedEngine", "start", "terminate_owned"]

XOLOTL_REPO_URL = "https://github.com/Indras-Mirror/Xolotl"
#: A cold DeepSeek-V4.1 load pins the arena from NVMe: ~50-92 s on the author's box.
XOLOTL_START_TIMEOUT_S = 900
#: The port every DeepSeek-V4.1 config in Xolotl's docs serves on.
XOLOTL_DEFAULT_PORT = 8150
#: exe stem -> the server shim that engine needs, relative to the root.
_SHIMS = {
    "ds41_generate": ("tools", "v41", "serve", "serve_ds41.py"),
    "glm_generate": ("tools", "glm", "serve", "serve_glm.py"),
}
#: Any of these running means a Xolotl or Strata engine already holds the GPU.
ENGINE_PROCESS_NAMES = frozenset({"ds41_generate", "glm_generate", "ds4_generate", "strata", "strata-dense"})
ARENA_DEFAULT_GIB = 60.0
POOL_DEFAULT_MIB = 8192
XOLOTL_OVERHEAD_GIB = 12.5
_GIB = 1 << 30


def xolotl_root(settings) -> Path:
    """The Xolotl checkout: the setting, else the folder LiteSuite's Model Hub names,
    else beside LiteSuite's other engines."""
    chosen = str(getattr(settings, "xolotl_root", "") or "").strip() or strata_engine.litesuite_folder("xolotlRoot")
    return Path(chosen) if chosen else ninfer_engine.litesuite_llm_dir() / "xolotl"


def server_python(root: Path) -> Path:
    """`.venv-serve` (the venv docs/WINDOWS.md makes for serving), else setup.py's `.venv`."""
    serve = root / ".venv-serve" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return serve if serve.is_file() else strata_engine.server_python(root)


def exe_stem(cfg: dict | None) -> str:
    """`ds41_generate` for `F:\\...\\build-win\\ds41_generate.exe`, on any OS."""
    name = re.split(r"[\\/]", str((cfg or {}).get("exe") or ""))[-1].lower()
    return name.removesuffix(".exe")


def server_script(root: Path, cfg: dict) -> Path:
    shim = _SHIMS.get(exe_stem(cfg))
    return root.joinpath(*shim) if shim else root / "serve" / "server.py"


def context_flag(cfg: dict | None) -> str:
    return "--ctx" if exe_stem(cfg) in _SHIMS else "--max-context"


def list_xolotl_configs(settings) -> list[Path]:
    """Every prepared model in the checkout, sorted. THE MODEL LIST FOR THIS BACKEND."""
    root = xolotl_root(settings)
    if not root.is_dir():
        return []
    return [p for p in sorted(root.glob("strata-*.json")) if read_config(p) is not None]


def xolotl_config(settings) -> Path | None:
    """The config to serve: the setting, else the ONE the checkout holds (two is not a guess)."""
    chosen = str(getattr(settings, "xolotl_config", "") or "").strip()
    if chosen:
        return Path(chosen)
    found = list_xolotl_configs(settings)
    return found[0] if len(found) == 1 else None


#: The names `StrataBackend` and `/model` call on a launcher (strata_engine has the same three).
install_root = xolotl_root
list_configs = list_xolotl_configs
chosen_config = xolotl_config


def config_context(cfg: dict | None) -> int | None:
    val = _arg_value(list((cfg or {}).get("args") or ()), context_flag(cfg))
    return int(val) if isinstance(val, str) and val.isdigit() else None


def default_host(settings) -> str:
    """The explicit setting, else the chosen config's port on loopback (8150 when none says)."""
    explicit = str(getattr(settings, "xolotl_host", "") or "").strip().rstrip("/")
    if explicit:
        return explicit
    path = xolotl_config(settings)
    cfg = read_config(path) if path else None
    port = cfg.get("port") if cfg else None
    return f"http://127.0.0.1:{port if isinstance(port, int) and port > 0 else XOLOTL_DEFAULT_PORT}"


def with_context(cfg: dict, max_context: int | None) -> dict:
    """`cfg` with its context flag replaced, or `cfg` itself when nothing changes. The engine
    refuses a context it cannot hold, in its own words; nothing here second-guesses it."""
    if not isinstance(max_context, int) or isinstance(max_context, bool) or max_context <= 0:
        return cfg
    if max_context == config_context(cfg):
        return cfg
    flag = context_flag(cfg)
    args = [str(a) for a in cfg["args"]]
    if flag in args[:-1]:
        args[args.index(flag) + 1] = str(max_context)
    else:
        args += [flag, str(max_context)]
    return {**cfg, "args": args}


# ── the memory gate ─────────────────────────────────────────────────────────

def pinned_bytes(cfg: dict) -> int:
    """RAM a DeepSeek-V4.1 start pins: arena + conversation pool + the engine's own share.
    0 when the config is not an arena engine or its numbers do not parse (cannot size)."""
    args = [str(a) for a in cfg.get("args") or ()]
    arena = _arg_value(args, "--arena-gib")
    if arena is None and exe_stem(cfg) != "ds41_generate":
        return 0
    pool = _arg_value(args, "--conversation-cache-mib")
    try:
        arena_gib = float(arena) if arena is not None else ARENA_DEFAULT_GIB
        pool_mib = int(pool) if pool is not None else POOL_DEFAULT_MIB
    except ValueError:
        return 0
    return int((arena_gib + XOLOTL_OVERHEAD_GIB) * _GIB) + max(0, pool_mib) * (1 << 20)


def memory_refusal(cfg: dict, *, ram_free: int | None, commit_free: int | None,
                   gpu: tuple[int, int] | None) -> str | None:
    """Why this config cannot be loaded into the memory that is free, or None. Pure."""
    if exe_stem(cfg) not in _SHIMS and "--arena-gib" not in [str(a) for a in cfg.get("args") or ()]:
        return strata_engine.memory_refusal(cfg, ram_free=ram_free, commit_free=commit_free, gpu=gpu)
    card = strata_engine.gpu_refusal(gpu)
    if card:
        return card
    pinned = pinned_bytes(cfg)
    if not pinned:
        return None
    return strata_engine.pin_refusal(
        "Xolotl", pinned, ram_free=ram_free, commit_free=commit_free, gpu=gpu,
        remedy="close other programs, or lower --arena-gib in its strata-<model>.json")


def engine_process_alive() -> bool:
    """Is any Xolotl or Strata engine running? True on doubt, so we never start a second."""
    try:
        import psutil
        return any(Path(p.info.get("name") or "").stem.lower() in ENGINE_PROCESS_NAMES
                   for p in psutil.process_iter(["name"]))
    except Exception:  # noqa: BLE001 - cannot ask -> assume alive, refuse to spawn
        return True


def refuse_reason(settings, cfg: dict, host: str, *, healthy) -> str | None:
    """Why a start must NOT happen, or None (the user 2026-09-10: check, ask, then load)."""
    if healthy(host):
        return f"a Xolotl server is already answering at {host} — attach to it; nothing was started."
    if engine_process_alive():
        return ("a Xolotl or Strata engine process is already running (still loading, or started "
                "elsewhere) — wait for it or stop it where it was started; nothing was started.")
    ram_free, commit_free = strata_engine.memory_free_bytes()
    return memory_refusal(cfg, ram_free=ram_free, commit_free=commit_free, gpu=strata_engine.gpu_memory_mib())


# ── the process we own ──────────────────────────────────────────────────────

def log_path() -> Path:
    from .paths import data_root
    return data_root() / ".xolotl" / "litetui-xolotl-server.log"


def launch_plan(settings) -> tuple[list[str], Path, dict, str]:
    """(argv, cwd, the config that will run, host) for the next start, or BackendError
    naming what is missing. Pure apart from reading the checkout; spawns nothing."""
    root = xolotl_root(settings)
    python = server_python(root)
    if not python.is_file() or not (root / "serve" / "server.py").is_file():
        raise BackendError(
            f"Xolotl is not installed at {root} — LiteSuite's Model Hub (Xolotl) points at a "
            f"checkout, or clone {XOLOTL_REPO_URL}, build it and make its serve venv "
            "(docs/WINDOWS.md), then set xolotl_root to that folder.")
    path = xolotl_config(settings)
    cfg = read_config(path) if path else None
    if cfg is None:
        raise BackendError(
            "no Xolotl model chosen — /model to pick one (each strata-<model>.json in the Xolotl "
            "folder is a prepared model), or set xolotl_config.")
    script = server_script(root, cfg)
    if not script.is_file():
        raise BackendError(f"{script} is missing — the Xolotl checkout at {root} is incomplete; nothing was started.")
    run_cfg = with_context(cfg, getattr(settings, "xolotl_max_context", None))
    run_path = path
    if run_cfg is not cfg:
        # A context LiteTUI chose: written beside the log, never over the checkout's own file.
        run_path = log_path().with_name(f"{path.stem}.litetui.json")
        run_path.parent.mkdir(parents=True, exist_ok=True)
        run_path.write_text(json.dumps(run_cfg, indent=1), encoding="utf-8")
    host = default_host(settings)
    port = host.rsplit(":", 1)[-1]
    if not port.isdigit():
        raise BackendError(f"xolotl_host {host} names no port — use http://127.0.0.1:<port>.")
    argv = [str(python), "-I", "-u", "-X", "utf8", str(script),
            "--engine", "strata", "--config", str(run_path), "--port", port]
    return argv, root, run_cfg, host


def start(settings, *, healthy, spawn=ttyguard.popen, notice=None) -> OwnedEngine:
    """Spawn Xolotl's server for the chosen config and wait for it to load (strata_engine's
    `spawn_server`, with this launcher's plan, gate, log and timeout)."""
    return strata_engine.spawn_server(settings, healthy=healthy, spawn=spawn, notice=notice, label="Xolotl",
                                      plan=launch_plan, refuse=refuse_reason, log=log_path,
                                      timeout=XOLOTL_START_TIMEOUT_S)
