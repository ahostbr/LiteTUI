"""Dual-backend model control — ONE seam between the app and its engine.

Two engines serve LiteTUI's OpenAI-compat chat stream; this module owns
everything about them that is NOT the chat stream:

  * ``lmstudio`` — LM Studio desktop at :1234. Chat is unchanged; the control
    plane (load with context length, unload) goes through the official
    ``lmstudio`` SDK instead of shelling out to ``lms load`` — no PATH
    dependency, no TTY risk, and unload/TTL become possible at all.
  * ``llamacpp`` — our own ``llama-server`` (installed by LiteSuite's Model
    Hub wizard into ~/.litesuite/llm) run in ROUTER mode: one process, no
    ``-m``, driven by a GENERATED ``--models-preset`` ini covering every GGUF
    discovered on the box. Model list/load/unload/switch are HTTP calls; the
    per-model Load-tab settings live in the ini.

Everything here was verified against the INSTALLED binary (cuda:b9360,
2026-08-21 spike) rather than trusted from blog posts:

  * ini schema: ``[section]`` is the model id; keys are the long option names
    without ``--``; ``model = <abs path>`` is required. Booleans are written
    ``key = true/false`` and the router translates to the positive/negative
    argv itself (``mmap = false`` → ``--no-mmap``). Value flags pass through
    (``flash-attn = on`` → ``--flash-attn on``).
  * ``GET /models`` → ``{"data": [{"id", "status": {"value":
    "unloaded"|"loading"|"loaded", "args": [...worker argv...]},
    "architecture": {"input_modalities": [...]}}]}``.
  * ``POST /models/load {"model": id}`` returns ``{"success": true}`` when the
    load has STARTED — the caller must poll ``/models`` until "loaded".
  * The router spawns each model as a CHILD process. Killing the router pid
    alone can orphan a worker: teardown is ``terminate()`` then a tree kill
    (``taskkill /T /F /PID``) — learned the hard way in the spike.

The no-load-on-connect law (app.py's _connect) extends here verbatim: the
router is started with ``--no-models-autoload`` and NOTHING in this module
loads weights except an explicit load()/apply_load_settings() call.

This module never imports app (re-accretion gate) and spawns children only
through ttyguard (envelope sweep rglobs src/**).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import paths
import ttyguard

# ── Where the engine lives (LiteSuite's Model Hub install) ───────────────────

LITESUITE_LLM = Path.home() / ".litesuite" / "llm"
LLAMA_EXE = LITESUITE_LLM / "bin" / "llama-server.exe"
BUILD_INFO = LITESUITE_LLM / "bin" / ".build-info"

#: Readiness/health poll ceiling for our own router process. The router itself
#: is up in ~1s (it loads no model); 30s covers a cold page cache.
ROUTER_START_TIMEOUT_S = 30
#: A model load is weights + KV allocation; 20 GB from a warm cache still
#: takes a while. Matches LiteSuite's STARTUP_TIMEOUT order of magnitude.
LOAD_TIMEOUT_S = 300


class BackendError(RuntimeError):
    """Raised with a message that ALREADY names the host/path it is about.

    The app renders these in-band. Naming the actual target inside the
    backend is what keeps the app.py:2364 lesson (never blame a host we did
    not try) true for three hosts instead of one.
    """


# ── The Load-tab vocabulary ──────────────────────────────────────────────────
# cfg-dict key → llama-server long option (ini key = the same string).
# The dict keys are THE canon: settings.llama_load_settings[model] uses them,
# the Model screen edits them, the ini generator translates them. One table.

VALUE_FLAGS: dict[str, str] = {
    "ctx": "ctx-size",
    "ngl": "n-gpu-layers",
    "threads": "threads",
    "batch": "batch-size",
    "ubatch": "ubatch-size",
    "parallel": "parallel",
    "seed": "seed",
    "rope_base": "rope-freq-base",
    "rope_scale": "rope-freq-scale",
    "cache_k": "cache-type-k",
    "cache_v": "cache-type-v",
    "flash_attn": "flash-attn",          # on | off | auto (a VALUE, not a toggle)
    # Canonical spec-decoding names. b9360 TOMBSTONED --draft-max/--draft-min
    # ("the argument has been removed") while keeping them LISTED in --help —
    # the census now skips tombstone lines, and we write only canonical names
    # so an alias's future removal cannot break a stored config.
    "draft_model": "spec-draft-model",
    "draft_min": "spec-draft-n-min",
    "draft_max": "spec-draft-n-max",
    "draft_p_min": "spec-draft-p-min",
    "mmproj": "mmproj",
    "chat_template_file": "chat-template-file",
}

#: Booleans. Written ``ini-key = true/false``; the router emits the
#: positive/negative argv form itself (verified in the encoding spike).
BOOL_FLAGS: dict[str, str] = {
    "mlock": "mlock",
    "mmap": "mmap",
    "kv_offload": "kv-offload",
    "kv_unified": "kv-unified",
    "context_shift": "context-shift",
}

FLAG_FOR: dict[str, str] = {**VALUE_FLAGS, **BOOL_FLAGS}


class IniUnexpressible(BackendError):
    """A cfg key exists but the INSTALLED build has no flag for it. The
    caller decides (dedicated spawn, or surface as n/a) — silently dropping
    a setting the user typed is the one forbidden outcome."""

    def __init__(self, key: str) -> None:
        super().__init__(f"load setting {key!r} has no flag in the installed llama-server")
        self.key = key


def parse_supported_flags(help_text: str) -> frozenset[str]:
    """Long-option names (sans --) present in ``llama-server --help``.

    The census is the B1 gate: every control the Model screen offers is
    backed by a flag PROVEN present, and a missing one renders as
    "n/a in installed build" instead of a knob that silently does nothing.

    🔴 A LISTED FLAG IS NOT A LIVE FLAG. b9360's help still prints removed
    arguments as stubs ("--draft-max N   the argument has been removed…"),
    and counting one cost a real hang: the ini carried --draft-max, the
    worker died at argv parse, and the router reported "loading" forever.
    A line that says the argument was removed contributes NOTHING.
    """
    import re

    flags: set[str] = set()
    for line in help_text.splitlines():
        if "has been removed" in line:
            continue
        flags.update(re.findall(r"--([a-z][a-z0-9-]*)", line))
    return frozenset(flags)


@lru_cache(maxsize=1)
def installed_flags() -> frozenset[str]:
    """Census of the ACTUAL installed binary, cached per process."""
    if not LLAMA_EXE.exists():
        return frozenset()
    try:
        proc = ttyguard.run([str(LLAMA_EXE), "--help"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return parse_supported_flags(proc.stdout or "")


def installed_build() -> str:
    """`cuda:b9360`-style tag from the wizard's .build-info, or "unknown"."""
    try:
        return BUILD_INFO.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


# ── Discovery ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelRow:
    key: str                 # picker id, cfg-dict key, ini section, request "model"
    path: str | None         # absolute GGUF path; None for LM Studio rows
    source: str              # litesuite | lmstudio-dir | hf-cache | custom | server
    loaded: bool = False
    extra_paths: tuple[str, ...] = ()   # same file found in other roots (Info tab)
    modalities: tuple[str, ...] = ()    # router architecture.input_modalities


#: Source precedence for dedupe — LiteSuite's copy is the shipped stack's.
_PRECEDENCE = {"litesuite": 0, "lmstudio-dir": 1, "hf-cache": 2, "custom": 3}

#: Files that are not standalone chat models. mmproj projectors are offered
#: separately (the Load tab's vision picker); shards beyond the first belong
#: to their -00001- sibling; tiny files are tokenizer/test debris.
_MIN_BYTES = 10 * 1024 * 1024

#: GGUF architectures that are NOT chat models. The E2E proved this filter's
#: necessity the hard way: LiteSuite's models dir holds VOICE ggufs (kokoro,
#: mimi, csm), and offering kokoro in /model let the router spend 300s
#: failing to serve a TTS codec as an LLM. Read from the file's own header —
#: never guessed from the name.
_NON_CHAT_ARCHS = frozenset({
    "clip", "whisper", "bert", "nomic-bert", "jina-bert-v2",
    "t5encoder", "wavtokenizer-dec", "kokoro", "mimi", "snac-dec", "csm",
    "flux",   # image-diffusion gguf — showed up in /model on the live walk
})


def _gguf_architecture(p: Path) -> str | None:
    """general.architecture from the GGUF header, or None when unreadable.

    Minimal parse of the real format (magic 'GGUF', v2/v3): walk the metadata
    KVs until the key appears — it is conventionally first, so this reads a
    few hundred bytes. Any surprise returns None: an unreadable file is
    LISTED (benefit of the doubt), and the load error then names it.
    """
    import struct

    try:
        with open(p, "rb") as f:
            if f.read(4) != b"GGUF":
                return None
            version = struct.unpack("<I", f.read(4))[0]
            if version < 2:
                return None
            _tensors, n_kv = struct.unpack("<QQ", f.read(16))

            def _string() -> str:
                (n,) = struct.unpack("<Q", f.read(8))
                return f.read(n).decode("utf-8", "replace")

            _SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
            for _ in range(min(n_kv, 64)):
                key = _string()
                (vtype,) = struct.unpack("<I", f.read(4))
                if vtype == 8:                       # string
                    val = _string()
                    if key == "general.architecture":
                        return val
                elif vtype == 9:                     # array — skip element-wise
                    (etype,) = struct.unpack("<I", f.read(4))
                    (count,) = struct.unpack("<Q", f.read(8))
                    if etype == 8:
                        for _i in range(count):
                            _string()
                    elif etype in _SIZES:
                        f.seek(_SIZES[etype] * count, 1)
                    else:
                        return None
                elif vtype in _SIZES:
                    f.seek(_SIZES[vtype], 1)
                else:
                    return None
    except (OSError, struct.error):
        return None
    return None


def _skip_gguf(p: Path) -> bool:
    name = p.name.lower()
    if "mmproj" in name:
        return True
    if "-of-" in name and "-00001-of-" not in name:
        return True
    try:
        if p.stat().st_size < _MIN_BYTES:
            return True
    except OSError:
        return True
    arch = _gguf_architecture(p)
    if arch is not None:
        if arch.lower() in _NON_CHAT_ARCHS:
            return True
        # Some non-LLM ggufs put a whole SENTENCE in the field (kyutai-mimi:
        # "this model cannot be used as LLM, use it via --model-vocoder…").
        # An architecture is an identifier; prose is a refusal.
        if " " in arch or len(arch) > 24:
            return True
    return False


def _scan_roots(settings) -> list[tuple[Path, str]]:
    roots: list[tuple[Path, str]] = []
    if settings.llama_scan_litesuite:
        roots.append((LITESUITE_LLM / "models", "litesuite"))
    if settings.llama_scan_lmstudio:
        roots.append((Path.home() / ".lmstudio" / "models", "lmstudio-dir"))
        roots.append((Path.home() / ".cache" / "lm-studio" / "models", "lmstudio-dir"))
    if settings.llama_scan_hf_cache:
        hf = Path(os.environ.get("HF_HOME") or (Path.home() / ".cache" / "huggingface"))
        roots.append((hf / "hub", "hf-cache"))
    for extra in settings.llama_models_dirs:
        roots.append((Path(extra).expanduser(), "custom"))
    return [(r, s) for r, s in roots if r.is_dir()]


def scan_models(settings) -> list[ModelRow]:
    """Every servable GGUF on the box, deduplicated, source-tagged.

    Dedupe identity is (file name, size): the same quant downloaded into two
    stores is one model, and listing it twice invites loading it twice.
    The higher-precedence copy wins; the loser's path survives on the row so
    the Info tab can show where else it lives.
    """
    best: dict[tuple[str, int], tuple[int, Path, str, list[str]]] = {}
    for root, source in _scan_roots(settings):
        for p in sorted(root.rglob("*.gguf")):
            if _skip_gguf(p):
                continue
            try:
                ident = (p.name.lower(), p.stat().st_size)
            except OSError:
                continue
            rank = _PRECEDENCE[source]
            prior = best.get(ident)
            if prior is None:
                best[ident] = (rank, p, source, [])
            elif rank < prior[0]:
                best[ident] = (rank, p, source, prior[3] + [str(prior[1])])
            else:
                prior[3].append(str(p))

    rows: list[ModelRow] = []
    seen: dict[str, int] = {}
    for _rank, p, source, extras in sorted(best.values(), key=lambda t: t[1].stem.lower()):
        key = p.stem.lower()
        # Two DIFFERENT files with one stem still need distinct ids —
        # a picker that shows one row for two models loads the wrong one.
        if key in seen:
            seen[key] += 1
            key = f"{key}~{seen[key]}"
        else:
            seen[key] = 1
        rows.append(ModelRow(key=key, path=str(p), source=source,
                             extra_paths=tuple(extras)))
    return rows


def mmproj_candidates(settings) -> list[str]:
    """Projector files for the Load tab's vision picker — the files the
    model scan deliberately skips."""
    out: list[str] = []
    for root, _source in _scan_roots(settings):
        out.extend(str(p) for p in sorted(root.rglob("*.gguf")) if "mmproj" in p.name.lower())
    return out


# ── Preset ini generation ────────────────────────────────────────────────────

def write_preset_ini(rows: list[ModelRow], settings, dest: Path | None = None) -> Path:
    """The router's whole model world, generated fresh from discovery + the
    per-model Load settings. Deterministic (sorted sections, sorted keys) so
    tests can diff it and two writes never disagree about anything real.

    Unknown cfg keys are REFUSED loudly (IniUnexpressible), and a key whose
    flag is missing from the installed build is refused the same way —
    writing an ini line the server will die on at load time would surface as
    a mystery load failure instead of a named setting.
    """
    dest = dest or (paths.LLAMA_DIR / "litetui-models.ini")
    dest.parent.mkdir(parents=True, exist_ok=True)
    flags = installed_flags()
    lines: list[str] = [
        "; generated by LiteTUI llm_backend — edit /modelcfg, not this file",
        f"; engine: {installed_build()}",
        "",
    ]
    for row in sorted(rows, key=lambda r: r.key):
        if row.path is None:
            continue
        lines.append(f"[{row.key}]")
        lines.append(f"model = {Path(row.path).as_posix()}")
        cfg = settings.llama_load_settings.get(row.key, {})
        for k in sorted(cfg):
            v = cfg[k]
            if v is None:
                continue
            ini_key = FLAG_FOR.get(k)
            if ini_key is None or (flags and ini_key not in flags):
                raise IniUnexpressible(k)
            if k in BOOL_FLAGS:
                lines.append(f"{ini_key} = {'true' if v else 'false'}")
            else:
                lines.append(f"{ini_key} = {v}")
        lines.append("")
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


# ── HTTP (sync bodies, called via asyncio.to_thread) ─────────────────────────

def _http_json(url: str, body: dict | None = None, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "LiteTUI", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _healthy(host: str, timeout: float = 2.0) -> bool:
    try:
        _http_json(f"{host}/health", timeout=timeout)
        return True
    except Exception:
        return False


# ── llama.cpp backend ────────────────────────────────────────────────────────

@dataclass
class _Owned:
    proc: object            # ttyguard.popen handle for OUR router (None = attached)
    log_path: Path
    log_file: object


class LlamaCppBackend:
    """Router-mode llama-server, attached or owned.

    Ownership rule: a healthy server on an attach host belongs to whoever
    started it (LiteSuite's Model Hub, usually) — we USE it and refuse to
    manage its load settings. Only a server WE spawned gets managed, and only
    processes we spawned get killed.
    """

    name = "llamacpp"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._host = settings.llama_host.rstrip("/")
        self._attached_host: str | None = None
        self._owned: _Owned | None = None
        self._rows: list[ModelRow] = []

    # -- identity ---------------------------------------------------------

    def base_url(self) -> str:
        return f"{self.host()}/v1"

    def host(self) -> str:
        return self._attached_host or self._host

    @property
    def attached(self) -> bool:
        return self._attached_host is not None

    # -- lifecycle --------------------------------------------------------

    async def ensure_running(self) -> str:
        return await asyncio.to_thread(self._ensure_running_sync)

    def _ensure_running_sync(self) -> str:
        if self._owned is not None and _healthy(self._host):
            return "ok"
        if _healthy(self._host):
            # Our port answers but we did not spawn it — a previous LiteTUI
            # or the user by hand. Treat as attached: never kill what we
            # cannot prove we own.
            self._attached_host = None
            return "ok"
        for cand in self._settings.llama_attach_hosts:
            cand = cand.rstrip("/")
            if _healthy(cand):
                self._attached_host = cand
                return f"attached {cand}"
        return self._spawn()

    def _spawn(self) -> str:
        if not LLAMA_EXE.exists():
            raise BackendError(
                f"llama-server not installed at {LLAMA_EXE} — run LiteSuite's "
                "Model Hub wizard to install the engine."
            )
        self._rows = scan_models(self._settings)
        if not self._rows:
            raise BackendError(
                "no GGUF models found in any scan root — download one in "
                "LiteSuite's Model Hub (or LM Studio), or add a folder in /settings."
            )
        ini = write_preset_ini(self._rows, self._settings)
        port = self._host.rsplit(":", 1)[-1]
        paths.LLAMA_DIR.mkdir(parents=True, exist_ok=True)
        log_path = paths.LLAMA_DIR / "litetui-llama-server.log"
        # The log FILE is the whole point: a server child of a TUI that
        # inherits the console shreds the UI (hard rule). ttyguard adds
        # CREATE_NO_WINDOW; the file gets both streams.
        log_file = open(log_path, "a", encoding="utf-8", errors="replace")
        log_file.write(f"\n--- litetui spawn {time.strftime('%F %T')} ---\n")
        log_file.flush()
        proc = ttyguard.popen(
            [
                str(LLAMA_EXE),
                "--models-preset", str(ini),
                "--host", "127.0.0.1",
                "--port", port,
                "--models-max", str(self._settings.llama_models_max),
                "--no-models-autoload",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(LLAMA_EXE.parent),   # the CUDA DLLs live beside the exe
        )
        deadline = time.monotonic() + ROUTER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if _healthy(self._host):
                self._owned = _Owned(proc, log_path, log_file)
                self._attached_host = None
                # Whatever way the app exits, OUR server dies with it — an
                # orphaned worker is a silent multi-GB VRAM leak. shutdown()
                # is idempotent, so a normal exit path calling it too is fine.
                import atexit
                atexit.register(self.shutdown)
                return f"spawned pid {proc.pid}"
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        tail = _log_tail(log_path)
        self._kill_tree(proc)
        log_file.close()
        raise BackendError(
            f"llama-server failed to become healthy on {self._host} — {tail}"
        )

    def shutdown(self) -> None:
        """Kill ONLY what we spawned. Router workers are child processes, so
        terminate() alone can orphan a loaded model holding VRAM — the tree
        kill is the fix the spike proved necessary."""
        if self._owned is None:
            return
        proc = self._owned.proc
        try:
            proc.terminate()
            for _ in range(10):
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
            if proc.poll() is None:
                self._kill_tree(proc)
        finally:
            try:
                self._owned.log_file.close()
            except OSError:
                pass
            self._owned = None

    @staticmethod
    def _kill_tree(proc) -> None:
        try:
            ttyguard.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    # -- models -----------------------------------------------------------

    async def list_models(self) -> list[ModelRow]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[ModelRow]:
        served = self._server_models()
        by_key = {r.key: r for r in scan_models(self._settings)}
        rows: list[ModelRow] = []
        for key, info in served.items():
            disk = by_key.pop(key, None)
            rows.append(ModelRow(
                key=key,
                path=disk.path if disk else _model_path_from_args(info),
                source=disk.source if disk else "server",
                loaded=info.get("status", {}).get("value") == "loaded",
                extra_paths=disk.extra_paths if disk else (),
                modalities=tuple(info.get("architecture", {}).get("input_modalities", ())),
            ))
        # Disk knows models the server does not (added since the ini was
        # written). They are still offered; loading one regenerates the ini
        # and restarts the router's world without touching resident models.
        rows.extend(by_key.values())
        rows.sort(key=lambda r: r.key)
        return rows

    def _server_models(self) -> dict[str, dict]:
        try:
            data = _http_json(f"{self.host()}/models")
        except Exception as e:
            raise BackendError(f"could not list models from {self.host()} — {e}") from e
        return {m["id"]: m for m in data.get("data", [])}

    async def model_info(self, key: str):
        return await asyncio.to_thread(self._model_info_sync, key)

    def _model_info_sync(self, key: str):
        """(window, type, loaded) with app._read_model_info's exact semantics:
        a NOT-loaded model must never report a window as if it were serving
        one. The worker argv echo is the source for the loaded window — it is
        what the worker was actually started with."""
        info = self._server_models().get(key)
        if info is None:
            return None
        loaded = info.get("status", {}).get("value") == "loaded"
        mtype = "vlm" if "image" in info.get("architecture", {}).get(
            "input_modalities", []) else "llm"
        args = info.get("status", {}).get("args", [])
        ctx = _arg_value(args, "--ctx-size")
        if loaded:
            return (int(ctx) if ctx else None), mtype, True
        return None, mtype, False

    # -- control ----------------------------------------------------------

    async def load(self, key: str, *, ctx: int | None = None) -> None:
        if ctx is not None:
            cfg = dict(self._settings.llama_load_settings.get(key, {}))
            cfg["ctx"] = ctx
            self._settings.llama_load_settings[key] = cfg
            await self.apply_load_settings(key, cfg)
            return
        await asyncio.to_thread(self._load_sync, key)

    def _refuse_if_attached(self, verb: str) -> None:
        if self.attached:
            raise BackendError(
                f"cannot {verb}: LiteSuite owns the server at {self.host()} — "
                "switch models in its Model Hub, or stop it and I'll run my own."
            )

    def _load_sync(self, key: str) -> None:
        self._refuse_if_attached("load a model")
        if key not in self._server_models():
            # New on disk since the ini was generated: rebuild the world.
            self._regen_ini()
        try:
            _http_json(f"{self.host()}/models/load", {"model": key}, timeout=30)
        except urllib.error.HTTPError as e:
            raise BackendError(
                f"load {key!r} refused by {self.host()} — {_body(e)}") from e
        except Exception as e:
            raise BackendError(f"load {key!r} failed against {self.host()} — {e}") from e
        # {"success": true} means STARTED (spike fact). Poll until loaded —
        # returning early hands the chat stream a 503.
        deadline = time.monotonic() + LOAD_TIMEOUT_S
        while time.monotonic() < deadline:
            info = self._server_models().get(key, {})
            state = info.get("status", {}).get("value")
            if state == "loaded":
                return
            if state not in ("loading", "loaded"):
                raise BackendError(
                    f"load {key!r} ended in state {state!r} — see "
                    f"{paths.LLAMA_DIR / 'litetui-llama-server.log'}"
                )
            # A worker that dies at argv parse leaves the ROUTER saying
            # "loading" forever (measured: an idle GPU and a 300s wait).
            # Our own log has the truth the router won't tell — read it.
            argv_err = _worker_argv_error()
            if argv_err:
                raise BackendError(
                    f"load {key!r}: the worker died parsing its arguments — "
                    f"{argv_err}"
                )
            time.sleep(1.0)
        raise BackendError(f"load {key!r} did not finish within {LOAD_TIMEOUT_S}s")

    async def unload(self, key: str) -> None:
        await asyncio.to_thread(self._unload_sync, key)

    def _unload_sync(self, key: str) -> None:
        self._refuse_if_attached("unload a model")
        try:
            _http_json(f"{self.host()}/models/unload", {"model": key}, timeout=60)
        except Exception as e:
            raise BackendError(f"unload {key!r} failed against {self.host()} — {e}") from e

    async def apply_load_settings(self, key: str, cfg: dict) -> None:
        await asyncio.to_thread(self._apply_sync, key, cfg)

    def _apply_sync(self, key: str, cfg: dict) -> None:
        """Persist cfg for KEY, rewrite the ini, and bounce only that model.
        The settings dict is the durable truth; the ini is derived output.

        🔴 UNLOAD BEFORE THE REGEN, NEVER AFTER. _regen_ini restarts the
        router, and a fresh router (no autoload) lists every model unloaded —
        an unload sent after the restart is a 400 for a model the new process
        never loaded, and it aborted the apply BEFORE the reload. Found by
        Ryan's manual pass on the first apply-to-a-LOADED-model; the E2E's
        apply had only ever run against a not-yet-loaded one."""
        self._refuse_if_attached("change load settings")
        self._settings.llama_load_settings[key] = dict(cfg)
        info = self._server_models().get(key)
        if info is not None and info.get("status", {}).get("value") == "loaded":
            self._unload_sync(key)   # evict while THIS router still knows it
        self._regen_ini()
        self._load_sync(key)

    def _regen_ini(self) -> None:
        rows = scan_models(self._settings)
        write_preset_ini(rows, self._settings)
        # The router reads the preset at startup; a changed world needs a
        # restart. Resident models are by definition ours (attached servers
        # are refused above), and the caller reloads the one it cares about.
        if self._owned is not None:
            self.shutdown()
        self._ensure_running_sync()

    # -- seat guard (studio tool suspend/resume) ---------------------------

    def seat_snapshot(self, key: str) -> dict | None:
        """seat_guard's record(): the live load config, or None when not
        loaded. Our router is app-private, and seat_guard runs BETWEEN
        completions by design, so a loaded model here is idle by
        construction — the queued-seats hazard is an LM Studio (parallel
        seats) shape, not ours."""
        if self.attached:
            return None
        try:
            info = self._server_models().get(key)
        except BackendError:
            return None
        if info is None or info.get("status", {}).get("value") != "loaded":
            return None
        args = info.get("status", {}).get("args", [])
        ctx = _arg_value(args, "--ctx-size")
        par = _arg_value(args, "--parallel")
        return {
            "identifier": key,
            "context": int(ctx) if ctx else None,
            "parallel": int(par) if par else None,
            "status": "idle",
            "queued": 0,
        }

    def seat_suspend(self, rec: dict) -> str | None:
        if self.attached:
            return (f"suspend unsupported: LiteSuite owns the server at "
                    f"{self.host()} — free VRAM from its Model Hub instead")
        try:
            self._unload_sync(rec["identifier"])
        except BackendError as e:
            return str(e)
        return None

    def seat_resume(self, rec: dict) -> str | None:
        try:
            self._load_sync(rec["identifier"])
        except BackendError as e:
            return str(e)
        got = self.seat_snapshot(rec["identifier"])
        if got is None:
            return "load reported success but the model is not listed as loaded"
        if rec.get("context") and got.get("context") != rec.get("context"):
            return (f"seat reloaded but at context {got.get('context')} "
                    f"instead of {rec['context']}")
        return None

    # -- per-request -------------------------------------------------------

    def request_overrides(self, key: str | None) -> dict:
        return _merged_overrides(self._settings, key)


# ── LM Studio backend ────────────────────────────────────────────────────────

class LMStudioBackend:
    """LM Studio desktop. Chat stays on the OpenAI-compat endpoint; control
    goes through the ``lmstudio`` SDK (imported lazily — a missing dep must
    degrade to a named in-band error, not kill boot). The native
    /api/v0/models REST read moves here from app.py byte-for-byte in
    semantics: it already encodes the loaded-window-vs-ceiling lesson."""

    name = "lmstudio"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._host = settings.lm_host.rstrip("/")
        self._sdk_ready = False

    def base_url(self) -> str:
        return f"{self._host}/v1"

    def host(self) -> str:
        return self._host

    @property
    def attached(self) -> bool:
        return False

    async def ensure_running(self) -> str:
        # LM Studio is a desktop app the USER starts; nothing to spawn.
        return "ok"

    def shutdown(self) -> None:
        pass

    def _sdk(self):
        try:
            import lmstudio  # noqa: PLC0415 — lazy on purpose (see class doc)
        except ImportError as e:
            raise BackendError(
                "the lmstudio SDK is not installed — `pip install lmstudio` "
                "(the llama.cpp backend works without it: /backend llamacpp)"
            ) from e
        if not self._sdk_ready:
            api_host = self._host.split("//", 1)[-1]
            try:
                lmstudio.configure_default_client(api_host)
            except Exception:
                # Already configured (one default client per process) — the
                # existing client is for the same host in every real run.
                pass
            try:
                lmstudio.set_sync_api_timeout(self._settings.lms_load_timeout_s)
            except Exception:
                pass
            self._sdk_ready = True
        return lmstudio

    # -- models -----------------------------------------------------------

    def _native_models(self) -> list[dict]:
        try:
            data = _http_json(f"{self._host}/api/v0/models", timeout=5)
        except Exception as e:
            raise BackendError(f"could not reach LM Studio at {self._host} — {e}") from e
        if isinstance(data, list):
            return data
        return data.get("models") or data.get("data") or []

    async def list_models(self) -> list[ModelRow]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[ModelRow]:
        rows = []
        for m in self._native_models():
            mid = m.get("id")
            if not mid:
                continue
            rows.append(ModelRow(
                key=mid, path=None, source="server",
                loaded=bool(m.get("loaded_context_length")),
            ))
        return rows

    async def model_info(self, key: str):
        return await asyncio.to_thread(self._model_info_sync, key)

    def _model_info_sync(self, key: str):
        """(window, type, loaded) — a ceiling is not a window (the qwen
        262,144-vs-8k lesson lives in app.py's docstring; the behavior now
        lives here)."""
        for m in self._native_models():
            if m.get("id") == key:
                loaded_len = m.get("loaded_context_length")
                if loaded_len:
                    return int(loaded_len), m.get("type"), True
                return int(m.get("max_context_length") or 0) or None, m.get("type"), False
        return None

    # -- control ----------------------------------------------------------

    async def load(self, key: str, *, ctx: int | None = None) -> None:
        def _load() -> None:
            lms = self._sdk()
            config = {"contextLength": ctx} if ctx else None
            try:
                lms.llm(key, config=config)
            except Exception as e:
                raise BackendError(
                    f"LM Studio load of {key!r} failed at {self._host} — {e}") from e
        await asyncio.to_thread(_load)

    async def unload(self, key: str) -> None:
        def _unload() -> None:
            lms = self._sdk()
            try:
                lms.llm(key).unload()
            except Exception as e:
                raise BackendError(
                    f"LM Studio unload of {key!r} failed at {self._host} — {e}") from e
        await asyncio.to_thread(_unload)

    async def apply_load_settings(self, key: str, cfg: dict) -> None:
        """The SDK can drive context length (and load fresh); the rest of the
        Load tab is LM Studio's own UI's job — refused HONESTLY so the screen
        can grey those fields rather than fake them."""
        sdk_side = {k for k in cfg if k == "ctx"}
        rest = {k for k, v in cfg.items() if k not in sdk_side and v is not None}
        if rest:
            raise BackendError(
                "LM Studio manages "
                + ", ".join(sorted(rest))
                + " in its own Load panel — only context length is scriptable here."
            )
        await self.load(key, ctx=cfg.get("ctx"))

    # -- seat guard --------------------------------------------------------

    def seat_snapshot(self, key: str) -> dict | None:
        """Kept on `lms ps --json` (read-only, through ttyguard): it is the
        one source of `status` and `queued` — the do-not-kill-other-seats
        gate. The SDK exposes neither today; revisit when it does."""
        import shutil

        lms = shutil.which("lms")
        if not lms:
            return None
        try:
            proc = ttyguard.run([lms, "ps", "--json"], timeout=20)
            rows = json.loads(proc.stdout or "[]")
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        for row in rows if isinstance(rows, list) else []:
            if row.get("identifier") == key:
                return {
                    "identifier": key,
                    "context": row.get("contextLength"),
                    "parallel": row.get("parallel"),
                    "status": row.get("status"),
                    "queued": row.get("queued", 0),
                }
        return None

    def seat_suspend(self, rec: dict) -> str | None:
        try:
            asyncio.run(self.unload(rec["identifier"]))
        except BackendError as e:
            return str(e)
        except RuntimeError:
            return "seat suspend called from a running event loop — use the async path"
        return None

    def seat_resume(self, rec: dict) -> str | None:
        try:
            asyncio.run(self.load(rec["identifier"], ctx=rec.get("context")))
        except BackendError as e:
            return str(e)
        except RuntimeError:
            return "seat resume called from a running event loop — use the async path"
        got = self.seat_snapshot(rec["identifier"])
        if got is None:
            return "load reported success but the model is not listed"
        if rec.get("context") and got.get("context") != rec.get("context"):
            return (f"seat reloaded but at context {got.get('context')} "
                    f"instead of {rec['context']}")
        return None

    # -- per-request -------------------------------------------------------

    def request_overrides(self, key: str | None) -> dict:
        return _merged_overrides(self._settings, key)


# ── Shared pieces ────────────────────────────────────────────────────────────

def _merged_overrides(settings, key: str | None) -> dict:
    """Global sampling defaults (settings.sampling_kwargs — the existing
    single home) with this model's Inference-tab overrides layered on top.
    An override set to None means 'force server default' and REMOVES the
    global value rather than sending null."""
    import settings as settings_mod

    merged = settings_mod.sampling_kwargs(settings)
    for k, v in settings.model_infer_overrides.get(key or "", {}).items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


#: Chat-completion params the OpenAI client accepts as REAL keyword args.
#: Everything else (top_k, min_p, repeat_penalty, chat_template_kwargs, ...)
#: must ride extra_body — the SDK's create() has typed params and NO **kwargs,
#: so an unknown top-level key is a TypeError, not a passthrough. (The old
#: `kwargs.update(sampling_kwargs(...))` call site would have crashed the
#: first time anyone set top_k; verified against openai 2.26.0.)
OPENAI_NATIVE_PARAMS = frozenset({
    "temperature", "top_p", "presence_penalty", "frequency_penalty",
    "seed", "stop", "max_tokens",
})


def split_request_kwargs(overrides: dict) -> tuple[dict, dict, dict | None]:
    """(native kwargs, extra_body, response_format|None) from a merged
    override dict. `json_schema` (a JSON string from the Inference tab)
    becomes a response_format; invalid JSON is refused HERE, loudly, because
    silently dropping a schema turns structured output into vibes."""
    native: dict = {}
    extra: dict = {}
    response_format = None
    for k, v in overrides.items():
        if k == "json_schema":
            if not v:
                continue
            try:
                schema = json.loads(v) if isinstance(v, str) else v
            except ValueError as e:
                raise BackendError(f"structured-output schema is not valid JSON — {e}") from e
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "litetui_schema", "schema": schema},
            }
        elif k == "enable_thinking":
            extra.setdefault("chat_template_kwargs", {})["enable_thinking"] = bool(v)
        elif k in OPENAI_NATIVE_PARAMS:
            native[k] = v
        else:
            extra[k] = v
    return native, extra, response_format


def _arg_value(args: list, flag: str) -> str | None:
    try:
        return args[list(args).index(flag) + 1]
    except (ValueError, IndexError):
        return None


def _model_path_from_args(info: dict) -> str | None:
    return _arg_value(info.get("status", {}).get("args", []), "--model")


def _body(e: "urllib.error.HTTPError") -> str:
    try:
        return e.read().decode("utf-8", "replace")[:300]
    except OSError:
        return str(e)


def _worker_argv_error(lines: int = 40) -> str | None:
    """The last argv-parse failure in OUR server log's tail, or None.

    Only the tail: an old error must not fail a NEW load that is genuinely
    in flight — the log is append-mode across spawns."""
    path = paths.LLAMA_DIR / "litetui-llama-server.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.strip().splitlines()[-lines:]):
        if "error while handling argument" in line:
            return line.split("]", 1)[-1].strip()
    return None


def _log_tail(path: Path, lines: int = 5) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no log)"
    return " ".join(text.strip().splitlines()[-lines:])[:400]


def llama_available() -> bool:
    """Is the llama backend worth offering? Installed engine, or any healthy
    attach target — the first-boot picker's detection."""
    return LLAMA_EXE.exists()


def make_backend(settings):
    """THE factory. app.py calls this once at boot and again on /backend."""
    if settings.backend == "llamacpp":
        return LlamaCppBackend(settings)
    return LMStudioBackend(settings)
