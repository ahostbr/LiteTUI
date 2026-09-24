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

🔴 THE LLAMA BACKEND SPEAKS TWO DIALECTS, BECAUSE llama-server IS TWO SERVERS.
Router mode is what we SPAWN. A ``llama-server -m <gguf>`` — SINGLE-MODEL mode
— is what LiteSuite runs, and it is the default attach target
(``settings.llama_attach_hosts`` → :8088), so the shape below is not an edge
case: it is what an attached session actually meets. Measured on the installed
binary (D12, 2026-08-22; fixtures in ``tests/fixtures/llama_single_model_*``,
re-derivable with ``e2e/llama_shapes_e2e.py``):

  * ``GET /models`` carries an OLLAMA-shaped ``models`` array beside ``data``.
    The ``data`` entries hold ``id``/``meta`` and have NO ``status`` and NO
    ``architecture`` — the two keys router parsing reads. A resident, serving
    model therefore parses as *not loaded* unless the shape is known.
  * ``id`` is the model ALIAS, which defaults to the GGUF's file name WITH the
    ``.gguf`` suffix, so it never equals discovery's ``path.stem``.
  * ``GET /props`` is the authority for a single-model server: ``model_alias``,
    ``model_path``, ``modalities.{vision,audio}``,
    ``default_generation_settings.n_ctx`` (the LIVE window, not the ceiling).
  * ``POST /models/load`` and ``POST /models/unload`` are **404 File Not
    Found**. The routes do not exist. Such a server serves exactly one model
    for its whole life and ignores the request's ``model`` field entirely.

The discriminator is the server's own answer: router ``/props`` says
``"role": "router"`` and ``"model_path": "none"``; single-model ``/props`` has
no ``role`` and a real ``model_path``. It is probed ONCE per connect
(``_probe_shape``) and the whole class keys off it — a single-model server is
always ``attached``, is never managed, and lists exactly one loaded row.

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
loads weights except an explicit load()/apply_load_settings() call —
``ensure_chat_ready`` included: it reads, waits, and refuses, but never loads.

``ensure_chat_ready(key)`` is the one query a caller must ask before opening a
chat stream (D2/D11). Without it the router's raw ``400 model is not loaded``
reaches the message widget verbatim; with it the caller gets either a turn
that can go out, or a BackendError already phrased for a human.

This module never imports app (re-accretion gate) and spawns children only
through ttyguard (envelope sweep rglobs src/**).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from litetui import paths
from litetui import router_record
from litetui import runtime_log
from litetui import ttyguard

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
    """Raised with a message phrased for a HUMAN, in plain words.

    The app renders these in-band (chat surface), so T137's rules apply at
    the SOURCE: no URLs, no WinError codes, no exception reprs — what seems
    wrong and what to do, in one or two sentences. Hosts, raw exceptions and
    log tails go to runtime_log.record_error beside the raise; a LOCAL file
    path stays when it is itself the action (the engine's own log, an install
    location). Naming which backend failed still keeps the app.py:2364 lesson
    (never blame a host we did not try) true for three hosts instead of one.
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


@dataclass(frozen=True)
class TerminalShutdown:
    """The result of stopping an OWNED engine. Guarantees are deliberately narrow so a
    caller cannot over-claim resource absence:

    - ``owned`` False: we held no handle (an ATTACHED or absent engine) — nothing was
      killed and nothing is claimed.
    - ``attempted``: we issued a kill on our owned process this call (a REQUEST, not
      evidence). A pre-dead process needs none, so attempted can be False with
      main_exited True.
    - ``main_exited``: poll() on the RETAINED process handle confirmed THIS process
      exited — the ONLY positive proof here. Never inferred from a pid lookup or a
      kill request.
    - ``tree`` is always 'unknown'. A whole-tree kill REQUEST (taskkill /T, or a
      KILL_ON_JOB_CLOSE job close) is not descendant-exit evidence: taskkill /T only
      issues signals, a job close is asynchronous, and jobkill exposes no
      active-process-count / empty-job probe (available/create/assign/close/alive
      only). So a main exit NEVER proves the worker tree drained — a caller must NOT
      settle broad VRAM/RAM absence. The field stays for a future descendant probe.
    - ``retained`` True: the outcome was uncertain and self._owned was KEPT — a retry
      re-drives the SAME handle (no rediscovery, so no pid-reuse ambiguity on the
      proof; the taskkill-by-pid REQUEST carries a documented residual reuse window).
    - ``error`` is an exception TYPE name only, never raw args.

    Process CREATION identity (to defeat pid reuse) is not exposed by the Popen /
    OwnedEngine handle, so it is intentionally absent; the retained handle itself,
    not a pid, is the identity here.
    """
    owned: bool
    attempted: bool = False
    main_exited: bool = False
    tree: str = "unknown"
    pid: int | None = None
    retained: bool = False
    error: str | None = None


def wait_for_exit(proc, timeout: float = 3.0, interval: float = 0.3) -> bool:
    """True iff poll() on THIS handle reports the process exited within `timeout`.
    Polls the retained child handle — never a pid lookup, so no pid-reuse ambiguity."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(interval)
    return proc.poll() is not None


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


def llama_executable(settings=None) -> Path:
    """Resolve the selected binary without quietly falling back on a typo."""
    selected = getattr(settings, "llama_executable", "") if settings is not None else ""
    if selected:
        path = Path(selected).expanduser().resolve()
        if not path.is_file():
            raise BackendError(f"The selected llama-server executable does not exist: {path}. Choose an installed executable or configure a server connection.")
        return path
    return LLAMA_EXE


@lru_cache(maxsize=1)
def installed_flags(executable: str | None = None) -> frozenset[str]:
    """Census of the ACTUAL installed binary, cached per process."""
    binary = Path(executable) if executable else LLAMA_EXE
    if not binary.exists():
        return frozenset()
    try:
        proc = ttyguard.run([str(binary), "--help"], timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    return parse_supported_flags(proc.stdout or "")


def installed_build(executable: str | None = None) -> str:
    """`cuda:b9360`-style tag from the wizard's .build-info, or "unknown"."""
    try:
        marker = Path(executable).parent / ".build-info" if executable else BUILD_INFO
        return marker.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def configured_flags(settings) -> frozenset[str]:
    selected = getattr(settings, "llama_executable", "")
    return installed_flags(str(llama_executable(settings))) if selected else installed_flags()


def configured_build(settings) -> str:
    selected = getattr(settings, "llama_executable", "")
    return installed_build(str(llama_executable(settings))) if selected else installed_build()


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


def _port_of(host: str) -> int | None:
    """The port from a `http://host:port` base url, or `None`.

    The record is per-PORT, so a record about 7470 says nothing about a
    session pointed at 8088. Comparing the whole host string instead would
    fail on `localhost` vs `127.0.0.1`, which are the same server.
    """
    try:
        return int(host.rsplit(":", 1)[-1])
    except ValueError:
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
        lowered = arch.lower()
        # MEASURED on this box, 2026-08-26: sesame-csm ships two >10 MB ggufs
        # whose header says `llama-csm`, not `csm`. Exact match let BOTH into
        # /model, where the router would spend its 300 s trying to serve a
        # speech codec as an LLM — the very cost this filter exists to avoid.
        # An architecture is hyphen-joined, so a NON-CHAT SEGMENT condemns the
        # whole id: `llama-csm` is a csm, whatever it is bolted to.
        if lowered in _NON_CHAT_ARCHS or set(lowered.split("-")) & _NON_CHAT_ARCHS:
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


#: THE ONE SPELLING OF "vision-capable model, deliberately WITHOUT a projector".
#:
#: 🔴 ABSENT AND NONE ARE DIFFERENT ANSWERS TO DIFFERENT QUESTIONS, and T231
#: only had the first. `_collect_group` POPS a cleared text field, so ABSENT
#: means "the user has not chosen" and the auto-pair guess correctly applies.
#: There was no way to say "I HAVE chosen, and the answer is nothing" — clearing
#: the field was exactly what re-enabled the guess. Ryan ruled this in on
#: 2026-09-03; the note in `write_preset_ini` that predicted it is replaced by
#: the thing itself.
#:
#: ⬜ ONE SPELLING, NOT A SET. Accepting "none"/"off"/"no"/"-" would be four
#: chances for the field's help text and the parser to disagree about which the
#: product means, and the help line names exactly what the parser accepts.
#: Compared case-insensitively and stripped, because a typed field is typed by a
#: human; not compared against a path, because a projector is always given as an
#: absolute path and `none` is not one.
NO_PROJECTOR = "none"


def is_no_projector(value) -> bool:
    """Is this stored `mmproj` the explicit "none", rather than a path?"""
    return isinstance(value, str) and value.strip().lower() == NO_PROJECTOR


def sibling_mmproj(model_path: str | Path | None) -> tuple[str | None, list[str]]:
    """The projector to pair with a model, and every candidate beside it.

    🔴 EXACTLY ONE, IN THE MODEL'S OWN DIRECTORY. Returns `(resolved, found)`;
    `resolved` is None unless `found` has length 1.

    WHY THIS EXISTS. `_skip_gguf` drops every `*mmproj*.gguf` from model
    discovery — correct, they are not chat models — and nothing then paired one
    back up. LM Studio does. Measured over ~/.lmstudio/models on 2026-09-03:
    NINE projectors, EIGHT with exactly one non-mmproj model in the same
    directory, and exactly ONE paired (by hand, in settings.json). The model
    loaded for a vision capture had its projector sitting beside it the whole
    time:

        unsloth/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_M.gguf   the model
        unsloth/Qwen3.8-27B-GGUF/mmproj-F16.gguf              never paired

    ⚠️ "EXACTLY ONE" IS A RULE ABOUT WHAT CANNOT BE DECIDED, NOT A FACT ABOUT
    THIS BOX — AND I CLAIMED THE OPPOSITE ONCE. My first measurement printed
    `parent.name` and showed two projectors under "Qwen3.8-27B-GGUF", which I
    reported as one directory holding two. It is TWO DIRECTORIES sharing a
    basename: `lmstudio-community/Qwen3.8-27B-GGUF` and
    `unsloth/Qwen3.8-27B-GGUF`, one projector and one model each. Re-derived by
    FULL PATH: nine directories hold a projector, ALL NINE hold exactly one,
    eight have exactly one model beside them, and one (`ggml-org/gemma-4-E2B-it-GGUF`)
    has none. Zero ambiguous directories today.

    The rule stands anyway, because the cost is asymmetric: a wrong projector
    does not fail loudly, it silently changes what the model can see. Two
    candidates cannot be ordered by ownership, so the answer is no guess and a
    caller that says why.

    📌 THE DIRECTORY, NOT A SEARCH. `mmproj_candidates` scans every root and
    returns a flat list — useful for a picker, useless for pairing, because the
    nearest projector by sort order is not the right one by ownership.
    """
    if model_path is None:
        return None, []
    try:
        parent = Path(model_path).parent
        found = sorted(
            str(p) for p in parent.glob("*.gguf") if "mmproj" in p.name.lower()
        )
    except OSError:
        return None, []
    return (found[0] if len(found) == 1 else None), found


def mmproj_candidates(settings) -> list[str]:
    """Projector files for the Load tab's vision picker — the files the
    model scan deliberately skips.

    ⚠️ CALLED BY NO PRODUCTION CODE as of 2026-09-03 —
    `grep -rn "mmproj_candidates" src/litetui` returns only this definition, and
    the "picker" its name promises is a plain text field. Kept because
    `tests/test_llama_discovery.py` pins it and a flat candidate list is what a
    real picker would want; NOT used for pairing — see `sibling_mmproj` for why
    a global list is the wrong instrument for that.
    """
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
    flags = configured_flags(settings)
    lines: list[str] = [
        "; generated by LiteTUI llm_backend — edit /modelcfg, not this file",
        f"; engine: {configured_build(settings)}",
        "",
    ]
    for row in sorted(rows, key=lambda r: r.key):
        if row.path is None:
            continue
        lines.append(f"[{row.key}]")
        lines.append(f"model = {Path(row.path).as_posix()}")
        cfg = settings.llama_load_settings.get(row.key, {})
        # 🔴 AUTO-PAIR A SIBLING PROJECTOR, ONLY WHEN THE KEY IS ABSENT.
        #
        # Absent is the honest test for "the user has not chosen", because
        # `_collect_group` POPS a cleared text field rather than storing a null
        # (model_switch.py: `if not raw: out.pop(key, None)`). Verified against
        # the live settings.json on 2026-09-03: of five models with load
        # settings, two carry an explicit mmproj path and three have NO KEY —
        # `grep -c '"mmproj": null'` returns 0. So an explicit path always wins
        # and this only fills a gap nobody has filled.
        #
        # THREE ANSWERS, NOT TWO (T245, ruled in by Ryan 2026-09-03 — the
        # follow-up the previous version of this comment predicted):
        #   absent          -> the user has not chosen; auto-pair the sibling
        #   NO_PROJECTOR    -> they HAVE chosen, and the answer is nothing
        #   a path          -> their path, always wins
        # The sentinel is DROPPED here rather than emitted: `mmproj = none`
        # would reach llama.cpp as a filename and fail at load with a message
        # about a missing file, which is the opposite of the user's intent.
        if is_no_projector(cfg.get("mmproj")):
            cfg = {k: v for k, v in cfg.items() if k != "mmproj"}
        elif "mmproj" not in cfg:
            auto, _found = sibling_mmproj(row.path)
            if auto:
                cfg = {**cfg, "mmproj": auto}
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


#: Which of llama-server's two control planes is answering. See the module
#: docstring: these are two different servers wearing one name, and the
#: difference is in what is POSSIBLE, not just in how JSON is spelled.
_SHAPE_ROUTER = "router"
_SHAPE_SINGLE = "single"


class VramRefused(BackendError):
    """A load the human declined, or that a headless child was not allowed to
    make on their behalf. A BackendError subclass so every existing caller
    already renders it as plain words (`_plain_backend_error` returns
    `str(e)`), and a distinct type so a caller that wants to tell "refused"
    from "failed" can."""


class _VramGate:
    """The one place a model load can be stopped (T690).

    🔴 IT LIVES ON THE BACKEND BECAUSE THE CALLERS ARE PLURAL AND GROWING. The
    first cut of this card gated ONE site — the pre-turn ensure-loaded path in
    `app.py` — and the most common swap in the app went straight past it:
    `plugins/model_switch.py` calls `backend.load()` directly for `/model
    <name>`, for the picker, AND for the T684 rpc `set_model`, and
    `apply_load_settings` is a reload nobody had counted as a load at all.
    Ryan asked for the modal *"when a 2nd instance tries to load a model IN ANY
    WAY ... EVERY TIME a model would be swapped or loaded"*, and a gate at a
    call site can only ever cover the callers that exist today.
        A RULE ENFORCED AT THE CALLERS IS A RULE WITH A LIST; A RULE ENFORCED AT
        THE CALLEE HAS NO LIST TO FALL OFF.

    ⚠️ REENTRANT BY CONSTRUCTION, because the public entries call each other.
    `LlamaCppBackend.load(ctx=...)` delegates to `apply_load_settings`, and
    LM Studio's `apply_load_settings` delegates to `load` — gating both without
    a guard would ask the same question twice for one user action.
    """

    #: Installed by the App: `async (model) -> bool`. None = no gate (every
    #: test that never installs one, and any embedder of this class).
    vram_gate = None
    _vram_asking = False

    #: Installed by the App (T691): `(model, cfg) -> None`, called whenever the
    #: load settings for a model are recorded, so the open conversation can keep
    #: its own copy. It sits beside the gate because it has the same shape — the
    #: WRITERS are several and most are inside this module, so a hook on the
    #: callee covers them with no list of callers to maintain.
    on_load_settings = None

    def set_settings(self, settings) -> None:
        """Re-point the captured Settings object at the App's current one.

        🔴 THE BACKEND SNAPSHOT IS THE BUG THE SETTINGS SCREEN KEEPS REHITTING.
        Every backend captures `settings` in `__init__` (the object the App
        handed it at construction). But the save path does NOT mutate that
        object — `SettingsBody._collect` builds a NEW one via `replace()` and
        `_on_settings_saved` rebinds `app.settings` to it. The backend is left
        holding the OLD object, so a saved `ninfer_max_context` never reaches
        `ninfer_engine.start` (which reads `self._settings`), and the engine
        spawns with the pre-save value — the "I set the context level and it
        did nothing" defect. `make_backend(app.settings)` on `/reconnect`
        rebuilds the reference, which is why the heavy path always worked and
        the light one never did. Re-pointing here restores the invariant the
        rest of the code relies on: `backend._settings is app.settings`.
        """
        self._settings = settings

    def _record_load_settings(self, key: str, cfg: dict) -> None:
        """Tell whoever is listening that `key` now loads with `cfg`."""
        hook = self.on_load_settings
        if hook is None:
            return
        try:
            hook(key, cfg)
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a load
            pass

    @asynccontextmanager
    async def vram_guard(self, key: str, *, reload: bool = False):
        """Ask once per outermost load, then run the body.

        `reload` is forwarded to the admission session so a reload re-admits an
        existing lease's peak rather than being counted as a fresh load. Only the
        OUTERMOST guard reaches admission (the reentrancy short-circuit below), so
        a load that delegates to a reload carries the outer call's flag, and a
        standalone `apply_load_settings` reaches admission with reload=True.
        """
        task = asyncio.current_task()
        admission = getattr(self, 'resource_admission', None)
        if (self.vram_gate is None and admission is None) or getattr(self, '_vram_owner', None) is task:
            yield
            return
        lock = getattr(self, '_vram_lock', None)
        if lock is None:
            lock = self._vram_lock = asyncio.Lock()
        await lock.acquire()
        self._vram_owner = task
        self._vram_asking = True
        try:
            allowed = await self.vram_gate(key) if self.vram_gate is not None else True
            if not allowed:
                raise VramRefused(
                    f"{key} was not loaded — another LiteTUI instance is running "
                    "and loading a different model would put a second model in VRAM."
                )
            if admission is not None:
                async with admission(key, reload=reload):
                    yield
            else:
                yield
        finally:
            self._vram_asking = False
            self._vram_owner = None
            lock.release()


class LlamaCppBackend(_VramGate):
    """llama-server: router-mode (ours) or single-model (someone else's).

    Ownership rule: a healthy server on an attach host belongs to whoever
    started it (LiteSuite's Model Hub, usually) — we USE it and refuse to
    manage its load settings. Only a server WE spawned gets managed, and only
    processes we spawned get killed.

    SHAPE rule (D12): we only ever spawn ROUTER mode, so a single-model server
    is by construction somebody else's process — it is always ``attached``,
    wherever it is answering, including on our own configured port. Its whole
    control plane differs (see the module docstring); ``_probe_shape`` decides
    once per connect and ``_list_sync`` / ``_model_info_sync`` /
    ``_refuse_if_attached`` / ``_chat_ready_sync`` each branch on it.
    """

    name = "llamacpp"
    #: The name a HUMAN reads in the header. Each backend carries its own
    #: (T861) because the header used to pick it out of a hand-listed map whose
    #: default was the literal string "LM Studio" — so NInfer, and anything
    #: added after that line was written, was announced as LM Studio.
    label = "llama.cpp"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._host = settings.llama_host.rstrip("/")
        self._attached_host: str | None = None
        #: Who owns the server we attached to, from `router.json`. `None` for
        #: a foreign server that wrote no record — which is exactly how a
        #: hand-started llama-server is recognised.
        self._attached_owner: str | None = None
        self._owned: _Owned | None = None
        self._rows: list[ModelRow] = []
        self._shape: str | None = None      # probed per connect, see _probe_shape

    # -- identity ---------------------------------------------------------

    def base_url(self) -> str:
        return f"{self.host()}/v1"

    def host(self) -> str:
        return self._attached_host or self._host

    @property
    def attached(self) -> bool:
        return self._attached_host is not None

    @property
    def attached_owner(self) -> str | None:
        """The owning app's name, or `None` when nobody claimed the router."""
        return self._attached_owner

    def owner_exited(self) -> bool:
        """True only when the app that owned our attached router has GONE.

        🔴 THREE CONDITIONS, AND EACH ONE EXCLUDES A CASE WE MUST NOT TAKE OVER.
        `attached` — our own dead server is a crash to report, not a takeover to
        perform. The record being absent (or dead) — a server can stop answering
        while its owner is very much alive, mid-restart or swapping a model, and
        spawning a second router on a port another app still claims is the exact
        collision `router_record` exists to prevent. The host being unhealthy —
        an owner that deregistered but left the server up is still serving us.

        ⚠️ REACHABLE BETWEEN LiteTUI AND LiteSuite, NOT BETWEEN TWO LiteTUIs.
        `router_record` knows two owners, "litesuite" and "litetui", so
        `is_mine` names the APP; a second LiteTUI reads the first's record as
        its own and never attaches in the first place. That is a separate
        defect — the record cannot tell a crashed instance's orphaned server
        from a live sibling's — and it is not fixed here.
        """
        if not self.attached:
            return False
        record = router_record.read()
        if record is not None and router_record.is_live(record):
            return False
        return not _healthy(self.host())

    def recover_owner_exit(self) -> str | None:
        """Become the owner after the previous one left. None when there is
        nothing to recover, so a caller can use it as its own guard.

        ⬜ IT DOES NOT TRY TO INHERIT THE SERVER — there is nothing left to
        inherit. `shutdown()` tree-kills what it spawned, so by the time this
        fires the process is gone and the port is free; `_ensure_running_sync`
        finds nothing healthy and spawns ours, which is the ordinary path and
        not a special case.
        """
        if not self.owner_exited():
            return None
        gone = self._attached_owner or "the other app"
        self._ensure_running_sync()
        if self.attached:
            # Somebody else got there first between the two checks. Say nothing
            # rather than claim a server we do not own.
            return None
        return f"{gone} closed its llama.cpp server — running my own instead."

    @property
    def single_model(self) -> bool:
        """True when the server serves ONE permanently-resident model.

        Reads the network on first use per connect, then answers from the
        cache — the shape cannot change without the process being replaced,
        and ``ensure_running`` clears the cache on every connect.
        """
        return self._probe_shape() == _SHAPE_SINGLE

    def _probe_shape(self) -> str:
        """Ask the server what it is. Router unless it says otherwise.

        ``/props`` is the server's own answer: router mode reports
        ``role="router"`` (with ``model_path: "none"``), single-model mode
        reports no ``role`` and a real ``model_path``. The ``/models``
        fallback is the router's per-model ``status`` object, which
        single-model entries do not have — belt and braces, both measured on
        b9360.

        Unrecognised servers resolve to ROUTER, which is the pre-D12
        behaviour: a guess must not quietly take management away from a
        server that supports it.
        """
        if self._shape is not None:
            return self._shape
        props: dict = {}
        try:
            got = _http_json(f"{self.host()}/props", timeout=5)
            if isinstance(got, dict):
                props = got
        except Exception:
            pass
        if props.get("role") == _SHAPE_ROUTER:
            shape = _SHAPE_ROUTER
        elif props.get("model_path") and props.get("model_path") != "none":
            shape = _SHAPE_SINGLE
        else:
            shape = _SHAPE_SINGLE if self._entries_lack_status() else _SHAPE_ROUTER
        self._shape = shape
        return shape

    def _entries_lack_status(self) -> bool:
        """/models fallback for a build whose /props tells us nothing.

        An EMPTY listing proves nothing (a router with an empty preset has no
        entries either), so it is not evidence of single-model mode.
        """
        try:
            data = _http_json(f"{self.host()}/models", timeout=5).get("data") or []
        except Exception:
            return False
        return bool(data) and not any("status" in m for m in data)

    def _props_sync(self) -> dict:
        """A FRESH /props read. Cheap, local, and touches no weights — the
        shape is cached, the values behind it deliberately are not."""
        try:
            got = _http_json(f"{self.host()}/props", timeout=5)
        except Exception as e:
            runtime_log.record_error(
                "llama.props_failed", detail=f"{self.host()}/props — {e}",
                site="llm_backend")
            raise BackendError(
                "the llama.cpp server did not answer its status check — wait a "
                "moment and try again.") from e
        return got if isinstance(got, dict) else {}

    # -- lifecycle --------------------------------------------------------

    async def ensure_running(self) -> str:
        return await asyncio.to_thread(self._ensure_running_sync)

    def _ensure_running_sync(self) -> str:
        llama_executable(self._settings)  # an explicit invalid choice is never ignored
        # A server can be replaced between connects, and a stale attach must
        # never decide which host the shape probe reads. Both are cleared
        # BEFORE anything is probed.
        self._shape = None
        self._attached_host = None
        self._attached_owner = None
        if self._owned is not None and _healthy(self._host):
            self._shape = _SHAPE_ROUTER      # we only ever spawn a router
            return "ok"
        if _healthy(self._host):
            # Our port answers but we did not spawn it — a previous LiteTUI,
            # the user by hand, or LiteSuite.
            #
            # 🔴 A SINGLE-MODEL SERVER HERE IS DEFINITIVELY NOT OURS: we only
            # ever spawn ROUTER mode. Attach to it, so the management
            # refusals that key on `attached` actually fire. This line used
            # to set None unconditionally while its own comment said "treat
            # as attached" — the code and the comment disagreed, and the
            # code won, which is the attach half of D12.
            #
            # 🔴 A LIVE OWNERSHIP RECORD SETTLES IT BEFORE ANY GUESSING.
            # Both apps default to 7470 and both know how to spawn a router
            # here, so "a router on our own port" is no longer evidence of
            # anything. If `router.json` names a live owner that is not us,
            # the server is THEIRS: attach, and never regenerate the ini or
            # restart it. Without that, LiteTUI would restart LiteSuite's
            # router out from under its GUI.
            # 🔴 LIVENESS IS EVALUATED BEFORE OWNERSHIP (T217), and the record
            # is DISCARDED when the process it names is gone. A dead record is
            # not a weaker claim, it is not a claim — so it must not survive
            # this line to be consulted by anything added below it later.
            #
            # ⚠️ HONEST ABOUT WHAT THIS DID AND DID NOT CHANGE: reordering the
            # terms of the old `and` chain changed NO outcome, because a
            # conjunction's value does not depend on its order and `record` was
            # not read after it. What is new is the `record = None`, which turns
            # "dead" into an absence for every later reader instead of relying
            # on each one to re-check. The two CONTROLs in
            # tests/test_router_coexistence.py already pin the outcomes; there
            # is no arm that can tell the reordering itself apart, and one that
            # claimed to would be measuring nothing.
            record = router_record.read()
            if record is not None and not router_record.is_live(record):
                record = None
            if (
                record is not None
                and (not record.is_mine or self._owned is None or self._owned.proc.pid != record.pid)
                and record.port == _port_of(self._host)
            ):
                self._attached_host = self._host
                self._attached_owner = record.owner
                return f"attached {self._host} (owner: {record.owner})"

            # A ROUTER on our own port is plausibly our own orphan (a crashed
            # LiteTUI leaves one), so it stays manageable — attaching would
            # lock the user out of loading anything until they killed it by
            # hand. A DEAD record lands here too: stale, ignored.
            if self._probe_shape() == _SHAPE_SINGLE:
                self._attached_host = self._host
            return "ok"
        for cand in self._settings.llama_attach_hosts:
            cand = cand.rstrip("/")
            if _healthy(cand):
                self._attached_host = cand
                self._shape = None           # re-probe against the ATTACHED host
                return f"attached {cand}"
        return self._spawn()

    def _spawn(self) -> str:
        binary = llama_executable(self._settings)
        if not binary.exists():
            raise BackendError(
                f"llama-server not installed at {binary}. Choose an installed "
                "llama-server executable or configure a supported server connection."
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
                str(binary),
                "--models-preset", str(ini),
                "--host", "127.0.0.1",
                "--port", port,
                "--models-max", str(self._settings.llama_models_max),
                "--no-models-autoload",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=str(binary.parent),   # the CUDA DLLs live beside the exe
        )
        deadline = time.monotonic() + ROUTER_START_TIMEOUT_S
        while time.monotonic() < deadline:
            if _healthy(self._host):
                self._owned = _Owned(proc, log_path, log_file)
                self._attached_host = None
                self._attached_owner = None
                self._shape = _SHAPE_ROUTER
                # 🔴 CLAIM ONLY WHAT IS ACTUALLY SERVING. The record is written
                # AFTER /health passes, never at popen time: a record naming a
                # process that is still starting — or that dies in the next
                # second — would make the other app attach to nothing and see
                # no models at all. A failed spawn writes nothing.
                try:
                    claimed = router_record.write(
                        pid=proc.pid,
                        port=_port_of(self._host) or int(port),
                        ini=str(ini),
                        build_tag=configured_build(self._settings),
                    )
                    if claimed is None:
                        # 🔴 REFUSED, NOT FAILED (T217). Someone else's LIVE
                        # claim on another port is in the file, and there is
                        # only one file — overwriting it would erase their
                        # ownership, and an erased claim reads as UNOWNED, so
                        # the next app to start would restart their live
                        # router. Our own spawn stands; we simply go
                        # unannounced, which costs coexistence for US and
                        # nothing for them. Logged because it is otherwise
                        # invisible: the router works and no record names it.
                        runtime_log.record(
                            "llama.router_record_kept_foreign",
                            detail=(
                                f"our router is up on {self._host} but "
                                "router.json holds a live foreign claim on "
                                "another port — left it theirs"
                            ),
                            site="llm_backend",
                        )
                except OSError:
                    # A router we cannot announce still works for US. Losing
                    # the record costs coexistence, not the session.
                    pass
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
        runtime_log.record_error(
            "llama.spawn_failed",
            detail=f"failed to become healthy on {self._host}\n{tail}",
            site="llm_backend")
        raise BackendError(
            "the llama.cpp engine did not become ready in time — see "
            f"{paths.LLAMA_DIR / 'litetui-llama-server.log'} for why, then try again."
        )

    def shutdown(self) -> "TerminalShutdown":
        """Production teardown path (atexit + UI). Delegates to shutdown_owned() so it
        uses the retained-handle exit proof instead of discarding the handle and
        returning None (the original gap). Kills ONLY what we spawned; an attached /
        no-owned server is left alone (owned=False). Sync and idempotent; callers may
        ignore the return."""
        return self.shutdown_owned()

    @staticmethod
    def _kill_tree(proc) -> None:
        try:
            ttyguard.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)], timeout=15
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def shutdown_owned(self) -> TerminalShutdown:
        """Stop our OWNED router and PROVE it exited by polling the RETAINED handle.

        Keeps self._owned until exit is confirmed on the same handle, returns a typed
        TerminalShutdown, and RETAINS the handle on any uncertain outcome so a retry
        re-drives it. An attached/absent server is never killed (owned=False). The
        production shutdown() delegates here.

        tree is always 'unknown': a terminate-only exit proves only the router, and
        taskkill /T is a kill REQUEST, not descendant-exit evidence — the caller must
        not free VRAM for the worker tree on a main-only exit.
        """
        owned = self._owned
        if owned is None:
            return TerminalShutdown(owned=False)
        proc = owned.proc
        pid = getattr(proc, "pid", None)
        attempted = False
        error = None
        try:
            if proc.poll() is None:
                proc.terminate()
                attempted = True
                if not wait_for_exit(proc):
                    # Best-effort whole-tree kill; taskkill /T is a REQUEST, never
                    # descendant-exit proof (and targets a PID that could be reused
                    # if the main exits in this window — a documented residual, not
                    # expanded on). llama-server has no job to contain the tree.
                    self._kill_tree(proc)
                    wait_for_exit(proc)
            main_exited = proc.poll() is not None
        except Exception as exc:                    # noqa: BLE001 - type-only diagnostic
            error = type(exc).__name__
            main_exited = False                     # never re-poll here; poll() may raise too
        if not main_exited:
            # Uncertain: RETAIN the handle (no cleanup, no clear) for a retry.
            return TerminalShutdown(owned=True, attempted=attempted, main_exited=False,
                                    tree="unknown", pid=pid, retained=True, error=error)
        if pid is not None:
            try:
                router_record.remove_if_mine(pid)
            except OSError:
                pass
        try:
            owned.log_file.close()
        except OSError:
            pass
        if self._owned is owned:                    # clear ONLY if still the same object
            self._owned = None
        return TerminalShutdown(owned=True, attempted=attempted, main_exited=True,
                                tree="unknown", pid=pid, retained=False, error=error)

    # -- models -----------------------------------------------------------

    async def list_models(self) -> list[ModelRow]:
        return await asyncio.to_thread(self._list_sync)

    def _list_sync(self) -> list[ModelRow]:
        if self._probe_shape() == _SHAPE_SINGLE:
            return self._single_rows()
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
            runtime_log.record_error(
                "llama.list_failed", detail=f"{self.host()}/models — {e}",
                site="llm_backend")
            raise BackendError(
                "could not read the model list from the llama.cpp server — wait a "
                "moment and try again.") from e
        return {m["id"]: m for m in data.get("data", [])}

    def _single_state(self) -> tuple[str, dict] | None:
        """(the served id, /props) for a single-model server, or None.

        The id comes from /models — it is what the server calls the model and
        what a request's ``model`` field is echoed as — and everything else
        from /props, which is the only place a single-model server states its
        path, its modalities and its LIVE window.
        """
        props = self._props_sync()
        try:
            key = next(iter(self._server_models()), None)
        except BackendError:
            key = None
        key = key or props.get("model_alias") or None
        return (key, props) if key else None

    def _single_rows(self) -> list[ModelRow]:
        """Exactly one row, marked loaded. No disk scan.

        A single-model server has no /models/load route at all (404,
        measured), so it serves this one model for its whole life. Offering
        the other GGUFs on the box would be offering a choice that cannot be
        honoured: picking one would change nothing and the next reply would
        still come from the resident model. The row is marked ``loaded``
        because it IS — the weights went in at startup, and the absence of a
        ``status`` object says nothing about residency in this dialect.
        """
        state = self._single_state()
        if state is None:
            return []
        key, props = state
        mods = props.get("modalities") or {}
        modalities = ["text"]
        if mods.get("vision"):
            modalities.append("image")
        if mods.get("audio"):
            modalities.append("audio")
        return [ModelRow(
            key=key,
            path=props.get("model_path") or None,
            source="server",
            loaded=True,
            modalities=tuple(modalities),
        )]

    async def model_info(self, key: str):
        return await asyncio.to_thread(self._model_info_sync, key)

    def _model_info_sync(self, key: str):
        """(window, type, loaded) with app._read_model_info's exact semantics:
        a NOT-loaded model must never report a window as if it were serving
        one. The worker argv echo is the source for the loaded window — it is
        what the worker was actually started with."""
        if self._probe_shape() == _SHAPE_SINGLE:
            # No argv echo in this dialect; /props states the live window
            # directly (n_ctx = what the server was started with, NOT
            # n_ctx_train, which is the ceiling).
            rows = self._single_rows()
            if not rows or rows[0].key != key:
                return None
            ctx = (self._props_sync().get("default_generation_settings")
                   or {}).get("n_ctx")
            mtype = "vlm" if "image" in rows[0].modalities else "llm"
            return (int(ctx) if ctx else None), mtype, True
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

    # -- readiness ---------------------------------------------------------

    async def ensure_chat_ready(self, key: str | None) -> None:
        await asyncio.to_thread(self._chat_ready_sync, key)

    def _chat_ready_sync(self, key: str | None) -> None:
        """Raise, in plain words, when KEY cannot serve a chat turn NOW.

        D2/D11: the caller opens a stream and the router answers ``400 model
        is not loaded``, which app.py renders verbatim into the message. Four
        different situations arrive as that one error (measured on b9360, see
        the module docstring), and they have different fixes:

          unloaded, in the preset  → /load it
          absent from the preset   → /model to something that is there
          no model selected at all → /model
          still LOADING            → nothing; it resolves by itself

        The last one is WAITED OUT rather than refused. Telling someone to
        /load a model that is loading is worse than saying nothing, and the
        wait ends the moment the router flips to ``loaded``.

        🔴 Nothing here starts a load. The no-load-on-connect law covers
        readiness checks: a question about state must not change it.
        """
        if not key:
            raise BackendError(
                "no model is selected — /model to pick one before sending.")
        if self._probe_shape() == _SHAPE_SINGLE:
            # One resident model, and the request's `model` field is ignored
            # outright (a chat naming a nonexistent model still answered 200).
            # There is nothing to load and nothing to refuse.
            return
        info = self._server_models().get(key)
        if info is None:
            raise BackendError(
                f"{key!r} is not on the llama.cpp server — "
                "/models to see what is, /model to switch."
            )
        deadline = time.monotonic() + LOAD_TIMEOUT_S
        while True:
            state = (info or {}).get("status", {}).get("value")
            if state == "loaded":
                return
            if state != "loading":
                raise BackendError(
                    f"{key!r} is not loaded — /load {key} first, or /model to "
                    "pick one that already is."
                )
            if time.monotonic() >= deadline:
                raise BackendError(
                    f"{key!r} was still loading after {LOAD_TIMEOUT_S}s — see "
                    f"{paths.LLAMA_DIR / 'litetui-llama-server.log'}"
                )
            time.sleep(1.0)
            info = self._server_models().get(key)

    # -- control ----------------------------------------------------------

    async def load(self, key: str, *, ctx: int | None = None, notice=None) -> None:
        """`notice()` fires ON A WORKER THREAD, after every refusal, immediately
        before the work — see the module note on T873. A UI caller marshals it
        back itself (`App.call_from_thread`)."""
        async with self.vram_guard(key):
            if ctx is not None:
                cfg = dict(self._settings.llama_load_settings.get(key, {}))
                cfg["ctx"] = ctx
                self._settings.llama_load_settings[key] = cfg
                self._record_load_settings(key, cfg)
                await self.apply_load_settings(key, cfg, notice=notice)
                return
            await asyncio.to_thread(self._load_sync, key, notice)

    def _owner_label(self) -> str:
        """Who to name in a refusal. The record when there is one, and a
        careful "another app" when there is not — a foreign server started by
        hand writes no record, and blaming LiteSuite for it would send the
        user to the wrong window."""
        if self._attached_owner == "litesuite":
            return "LiteSuite"
        if self._attached_owner:
            return self._attached_owner
        return "another app"

    def _refuse_if_attached(self, verb: str) -> None:
        if not self.attached:
            return
        # 🔴 AN ADOPTED ROUTER IS SHARED, NOT OFF LIMITS — BUT ONLY ONE THAT
        # SOMEBODY SIGNED FOR. Hot-loading a model a cooperating app's router
        # already has in its preset is exactly what the route is for, and both
        # apps see the result: that is the coexistence the record buys.
        #
        # ⚠️ THE RECORD IS THE PERMISSION, NOT THE SHAPE. My first cut keyed
        # this on "is it a router" alone, which silently granted management
        # over ANY foreign router — including one started by hand that nobody
        # vouched for — and `test_attached_refuses_management` caught it.
        # A router with no record stays off limits, as it always was.
        #
        # What stays refused even for a cooperating owner is rewriting the ini
        # or restarting the process: that file belongs to whoever spawned it.
        if self._probe_shape() == _SHAPE_ROUTER and self._attached_owner is not None:
            return
        if self._probe_shape() == _SHAPE_SINGLE:
            # Say WHY, and say it before the request goes out: /models/load
            # is 404 here, and "load refused — File Not Found" would blame
            # the model for a property of the server.
            raise BackendError(
                f"cannot {verb}: that server is serving one model and cannot "
                "switch — it was started with a single -m <gguf>, so it has no "
                "load or unload route at all. Change the model where that "
                "server was started (LiteSuite's Model Hub), or stop it and "
                "I'll run my own router."
            )
        raise BackendError(
            f"cannot {verb}: {self._owner_label()} owns that server — switch "
            "models there, or stop it and I'll run my own."
        )

    def eviction_notice(self, key: str) -> str | None:
        """What this load is about to push out, or None when it pushes nothing.

        🔴 THE CEILING IS SHARED AND BELONGS TO WHOEVER SPAWNED THE ROUTER.
        `--models-max` comes from the OWNER's `llama_models_max`, so a second
        instance loading a model can evict one the first is mid-turn on, and the
        router does it in silence — `/models/load` answers `{"success": true}`
        either way. Naming it in the loading instance's status line is the whole
        countermeasure: both sides read the same `/models`, so the other one
        sees the change on its next refresh without any new protocol between
        two apps that today share nothing but a port.

        ⚠️ IT DOES NOT NAME *WHICH* MODEL WILL BE DROPPED. That policy is
        llama.cpp's, not ours; "evicting X" when the router actually drops Y is
        a confident wrong answer, and worse than a vague right one. It names
        what is loaded and the ceiling that forces a choice.

        ⚠️ AND IT NEVER RAISES. It runs on the way into a load, so a probe that
        threw would turn a load that was about to succeed into a failure — the
        warning is worth less than the thing it interrupts.
        """
        try:
            rows = self._server_models()
        except Exception:  # noqa: BLE001 - see the docstring
            return None
        loaded = [
            mid for mid, row in rows.items()
            if (row.get("status") or {}).get("value") == "loaded"
        ]
        if key in loaded:
            return None
        ceiling = int(self._settings.llama_models_max or 0)
        if ceiling <= 0 or len(loaded) < ceiling:
            return None
        return (
            f"loading {key} will unload one of {', '.join(sorted(loaded))} — "
            f"this llama.cpp server holds {ceiling} at a time, and another app "
            "may be using it."
        )

    def _load_sync(self, key: str, notice=None) -> None:
        self._refuse_if_attached("load a model")
        if key not in self._server_models():
            # New on disk since the ini was generated: rebuild the world.
            self._regen_ini()
        # 🔴 T873. The caller used to print "Loading <key>…" BEFORE awaiting this,
        # so on an adopted or single-model server the user read a promise and
        # then `_refuse_if_attached`'s contradiction. Two guards can still refuse
        # above this line (`_refuse_if_attached`, and `_regen_ini` on an adopted
        # router), so the announcement belongs HERE — after them, before the
        # request that is the work.
        if notice is not None:
            notice()
        try:
            _http_json(f"{self.host()}/models/load", {"model": key}, timeout=30)
        except urllib.error.HTTPError as e:
            runtime_log.record_error(
                "llama.load_refused", detail=f"{self.host()}/models/load — {_body(e)}",
                site="llm_backend")
            raise BackendError(
                f"the llama.cpp server refused to load {key!r} — /models to see what it has.") from e
        except Exception as e:
            runtime_log.record_error(
                "llama.load_failed", detail=f"{self.host()}/models/load — {e}",
                site="llm_backend")
            raise BackendError(
                f"could not load {key!r} on the llama.cpp server — try again in a moment.") from e
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
                runtime_log.record_error(
                    "llama.worker_argv", detail=f"load {key!r}:\n{argv_err}",
                    site="llm_backend")
                raise BackendError(
                    f"could not start {key!r} on the llama.cpp server — see "
                    f"{paths.LLAMA_DIR / 'litetui-llama-server.log'} for why."
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
            runtime_log.record_error(
                "llama.unload_failed", detail=f"{self.host()}/models/unload — {e}",
                site="llm_backend")
            raise BackendError(f"could not unload {key!r} on the llama.cpp server — try again in a moment.") from e

    async def apply_load_settings(self, key: str, cfg: dict, *, notice=None) -> None:
        # A reload IS a load: it puts the weights back with a new window. It is
        # a reload to admission (re-admits the existing lease's peak), not a fresh
        # load — so nothing double-counts the same model's capacity.
        async with self.vram_guard(key, reload=True):
            await asyncio.to_thread(self._apply_sync, key, cfg, notice)

    def _apply_sync(self, key: str, cfg: dict, notice=None) -> None:
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
        self._record_load_settings(key, dict(cfg))
        info = self._server_models().get(key)
        if info is not None and info.get("status", {}).get("value") == "loaded":
            self._unload_sync(key)   # evict while THIS router still knows it
        self._regen_ini()
        # 🔴 T873, and AFTER `_regen_ini` on purpose. `_refuse_if_attached`
        # RETURNS for an adopted router somebody signed for, and `_regen_ini`
        # then refuses it anyway ("the preset belongs to it") — so a notice
        # placed after the first guard alone would still be contradicted by the
        # second. `_load_sync` is passed no notice: the reload below is this
        # call's work, and it must not announce itself a second time.
        if notice is not None:
            notice()
        self._load_sync(key)

    def _regen_ini(self) -> None:
        # 🔴 THE INI BELONGS TO WHOEVER SPAWNED THE ROUTER. Regenerating it
        # while attached would rewrite another app's preset and then restart
        # its process — the precise accident `router.json` exists to prevent.
        # Loads are allowed on an adopted router; this is not a load.
        if self.attached:
            raise BackendError(
                f"cannot rebuild the model preset: {self._owner_label()} owns "
                "that server and its preset belongs to it. Add the model "
                "there, or stop that server and I'll run my own."
            )
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
            return (f"suspend unsupported: {self._owner_label()} owns that "
                    "server — free VRAM from its own model manager instead")
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

class LMStudioBackend(_VramGate):
    """LM Studio desktop. Chat stays on the OpenAI-compat endpoint; control
    goes through the ``lmstudio`` SDK (imported lazily — a missing dep must
    degrade to a named in-band error, not kill boot). The native
    /api/v0/models REST read moves here from app.py byte-for-byte in
    semantics: it already encodes the loaded-window-vs-ceiling lesson."""

    name = "lmstudio"
    #: The name a HUMAN reads in the header. Each backend carries its own
    #: (T861) because the header used to pick it out of a hand-listed map whose
    #: default was the literal string "LM Studio" — so NInfer, and anything
    #: added after that line was written, was announced as LM Studio.
    label = "LM Studio"

    def __init__(self, settings) -> None:
        self._settings = settings
        self._host = settings.lm_host.rstrip("/")
        from threading import RLock
        self._sdk_lock = RLock()
        self._sdk_closed = False
        self._sdk_session = None

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
        with self._sdk_lock:
            self._sdk_closed = True
            session = self._sdk_session
        if session is not None and not session.close():
            runtime_log.record_error(
                "lmstudio.cleanup_pending", site="llm_backend",
                detail=session.cleanup_error or "SDK operation still settling; connection cleanup deferred",
            )

    def _sdk(self):
        with self._sdk_lock:
            if self._sdk_closed:
                raise BackendError("LM Studio connection is closed — reconnect before loading.")
            if self._sdk_session is None:
                try:
                    from litetui.lmstudio_session import LMStudioSession
                    self._sdk_session = LMStudioSession(
                        self._host.split("//", 1)[-1],
                        timeout=self._settings.lms_load_timeout_s,
                    )
                except ImportError as e:
                    raise BackendError(
                        "the lmstudio SDK is not installed — `pip install lmstudio` "
                        "(the llama.cpp backend works without it: /backend llamacpp)"
                    ) from e
            self._sdk_session.timeout = self._settings.lms_load_timeout_s
            return self._sdk_session

    # -- models -----------------------------------------------------------

    def _native_models(self) -> list[dict]:
        try:
            data = _http_json(f"{self._host}/api/v0/models", timeout=5)
        except Exception as e:
            runtime_log.record_error(
                "lmstudio.list_failed", detail=f"{self._host}/api/v0/models — {e}",
                site="llm_backend")
            raise BackendError(
                "could not read the model list from LM Studio — try again in a moment.") from e
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

    def loaded_models(self) -> list[str]:
        """Ids LM Studio has RESIDENT right now. Read-only, starts no load.

        Reads the same native listing `_list_sync` does, so it cannot
        disagree with it, and asks the only question a headless caller
        needs: what is already in VRAM.
        """
        return [
            str(m.get("id"))
            for m in self._native_models()
            if m.get("id") and m.get("loaded_context_length")
        ]

    # -- readiness ---------------------------------------------------------

    async def ensure_chat_ready(self, key: str | None) -> None:
        await asyncio.to_thread(self._chat_ready_sync, key)

    def _chat_ready_sync(self, key: str | None) -> None:
        """The same question, and for LM Studio the answer is nearly always
        yes — deliberately.

        🔴 A COLD MODEL IS NOT AN ERROR HERE. LM Studio JIT-loads on the first
        request; refusing one would break chats that work today, which is the
        opposite of what D2/D11 asked for. The only thing this can rule out
        is a model LM Studio has never downloaded — its native listing covers
        every downloaded model, so absence from it is decisive.

        The asymmetry with the llama.cpp side is the truth about the two
        engines, not an oversight: our router will not JIT-load (it is
        started ``--no-models-autoload``, by law), and LM Studio will.

        ⚠️ AND THAT IS TRUE OF AN INTERACTIVE SESSION ONLY (T594). A person
        typing a prompt wants the load; a `--rpc` child has nobody watching
        and must never take VRAM someone else is using, so app.py refuses
        the cold case BEFORE this is reached in headless mode. The decision
        above is unchanged for the path it was written for.
        """
        if not key:
            raise BackendError(
                "no model is selected — /model to pick one before sending.")
        if key not in {m.get("id") for m in self._native_models()}:
            raise BackendError(
                f"{key!r} is not downloaded in LM Studio — "
                "/models to see what is, /model to switch."
            )

    # -- control ----------------------------------------------------------

    async def load(self, key: str, *, ctx: int | None = None, notice=None, _reload: bool = False) -> None:
        def _load() -> None:
            lms = self._sdk()
            config = {"contextLength": ctx} if ctx else None
            # T873: after `_sdk()`, which is the last thing here that can refuse
            # before the load itself. This backend has no attached-server guard
            # — the promise was never wrong here — but the caller passes ONE
            # notice to whichever backend answers, so it must fire on all of them
            # or LM Studio users lose the line entirely.
            if notice is not None:
                notice()
            try:
                lms.load(key, config=config)
            except Exception as e:
                runtime_log.record_error(
                    "lmstudio.load_failed", detail=f"load of {key!r} at {self._host} — {e}",
                    site="llm_backend")
                raise BackendError(f"could not load {key!r} in LM Studio — try again in a moment.") from e
        async with self.vram_guard(key, reload=_reload):
            await asyncio.to_thread(_load)
        # LM Studio takes its load config on the request itself, so THIS is the
        # point at which "what this model was loaded with" is known.
        self._record_load_settings(key, {"ctx": ctx} if ctx else {})

    async def unload(self, key: str) -> None:
        def _unload() -> None:
            lms = self._sdk()
            try:
                lms.unload(key)
            except Exception as e:
                runtime_log.record_error(
                    "lmstudio.unload_failed", detail=f"unload of {key!r} at {self._host} — {e}",
                    site="llm_backend")
                raise BackendError(f"could not unload {key!r} in LM Studio — try again in a moment.") from e
        await asyncio.to_thread(_unload)

    async def apply_load_settings(self, key: str, cfg: dict, *, notice=None) -> None:
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
        # No guard here: this delegates to `load`, which has one. Wrapping both
        # would ask twice for one action — see `_VramGate`.
        # The `rest` refusal above is already ahead of the notice `load` fires,
        # so this path was never the T873 shape; the parameter only keeps the
        # signature uniform for a caller that hands one to any backend.
        await self.load(key, ctx=cfg.get("ctx"), notice=notice, _reload=True)

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
    from litetui import settings as settings_mod

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
                runtime_log.record_error(
                    "settings.bad_schema", detail=f"json_schema: {e}",
                    site="llm_backend")
                raise BackendError(
                    "the structured-output schema in your model settings is not valid "
                    "JSON — fix it in the Inference tab.") from e
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


def llama_available(settings=None) -> bool:
    """Is the llama backend worth offering? Installed engine, or any healthy
    attach target — the first-boot picker's detection."""
    return llama_executable(settings).is_file()


#: The gate every new backend is born with. The App installs it once via
#: `set_vram_gate`; `make_backend` stamps it onto each instance.
_DEFAULT_VRAM_GATE = None

#: The load-settings listener new backends are born with (T691). Installed the
#: same way and for the same reason: `app.backend = make_backend(...)` happens
#: at four sites, so the App's own assignment is not the only one.
_DEFAULT_LOAD_HOOK = None


def set_load_settings_hook(hook) -> None:
    """Install the per-conversation load-settings listener (T691)."""
    global _DEFAULT_LOAD_HOOK
    _DEFAULT_LOAD_HOOK = hook


def set_vram_gate(gate) -> None:
    """Install the second-instance VRAM gate for backends made from here on.

    🔴 ONE INSTALL, BECAUSE THERE ARE FOUR ASSIGNMENT SITES AND THREE ARE IN A
    PLUGIN. `app.backend = make_backend(...)` happens once at boot and three
    more times in `plugins/model_switch.py` (a `/backend` switch, and two
    recovery paths). Installing the hook at the App's own assignment would have
    meant a backend SWITCH silently dropped the gate — the same "enforced at
    the callers, so it has a list to fall off" mistake this card already made
    once at the call sites. The factory is the single door every one of them
    goes through.

    ⚠️ STAMPED PER INSTANCE, NOT READ FROM MODULE STATE AT CALL TIME. A backend
    keeps the gate it was born with, so a later App installing its own does not
    reach back into an older App's backend — the suite builds many Apps in one
    process, and module-level state read live is how they start answering for
    each other.
    """
    global _DEFAULT_VRAM_GATE
    _DEFAULT_VRAM_GATE = gate


#: THE backend list — the one door. /backend, the Settings "Engine" select,
#: gui_rpc validation and make_backend all read this; none carries its own copy
#: (Ryan 2026-09-17: "modular reusable pieces that connect", not a fourth
#: if-chain per backend). Order is display order.
BACKENDS: tuple[tuple[str, str], ...] = (
    ("custom", "Custom (OpenAI-compatible server)"),
    ("lmstudio", "LM Studio desktop"),
    ("llamacpp", "llama.cpp (our own llama-server)"),
    ("ninfer", "NInfer (NVFP4 5090 engine)"),
    ("codex", "Codex (OAuth subscription)"),
    ("claude", "Claude Agent"),
)
BACKEND_NAMES: tuple[str, ...] = tuple(name for name, _ in BACKENDS)


def backend_label(name: str) -> str:
    return dict(BACKENDS).get(name, name)


def visible_backends() -> tuple[tuple[str, str], ...]:
    """BACKENDS minus NInfer on a box that is not an RTX 5090 (T893 — Ryan:
    "the user never sees anything about ninfer"). Every picker lists THIS."""
    from litetui import gpu_gate

    if gpu_gate.is_rtx_5090():
        return BACKENDS
    return tuple(b for b in BACKENDS if b[0] != "ninfer")


def backend_hint(backend) -> str:
    """The same question, asked of a BACKEND rather than an app: what to say
    when it cannot be reached or has no models to offer.

    Split out for T862: `_plain_backend_error` in app.py is a pure function
    with no app to hand, and it needs this channel too — a connection failure
    on NInfer said "start it, or check /backend", and neither of those is a
    step the user can take.

    🔴 T889: this lives in the BACKEND LAYER, not in the `model_switch`
    plugin. app.py may import the plugin substrate and nothing below it
    (test_plugin_dogfood gates that), and this is the one answer it must
    reach for two user-facing messages — so its home must be a module app.py
    already owns. It is NOT in the substrate `litetui/plugins/__init__`:
    importing model_switch there would drag a plugin into package init and
    invert the layering. Asked of the backend by `getattr`, not added to a
    base class: a backend that says nothing keeps today's words exactly, which
    is what stops this being a global find-and-replace.
    """
    hint = getattr(backend, "empty_state_hint", None)
    try:
        text = hint() if callable(hint) else ""
    except Exception:  # noqa: BLE001 - a hint must never break the command
        text = ""
    return str(text or "try /reconnect")


def make_backend(settings):
    """THE factory. app.py calls this once at boot and again on /backend."""
    backend = _make_backend(settings)
    if _DEFAULT_VRAM_GATE is not None:
        backend.vram_gate = _DEFAULT_VRAM_GATE
    if _DEFAULT_LOAD_HOOK is not None:
        backend.on_load_settings = _DEFAULT_LOAD_HOOK
    return backend


def _make_backend(settings):
    if settings.backend == "claude":
        from litetui.claude_backend import ClaudeBackend
        return ClaudeBackend(settings)
    if settings.backend == "custom":
        from litetui.custom_backend import CustomBackend
        return CustomBackend(settings)
    if settings.backend == "codex":
        from litetui.oauth_backend import OAuthBackend
        return OAuthBackend(settings)
    if settings.backend == "llamacpp":
        return LlamaCppBackend(settings)
    if settings.backend == "ninfer":
        from litetui import gpu_gate

        if not gpu_gate.is_rtx_5090():
            # An explicit/saved provider choice must never silently become
            # another engine, especially when restoring a conversation.
            raise BackendError(
                "NInfer requires supported RTX 5090 hardware. "
                "Choose another backend explicitly; no fallback was used."
            )
        # 🔴 T806 — RYAN: *"LITETUI WAS ALWAYS THE END GOAL FOR NINFER"*.
        # Imported here rather than at module scope so a broken NInfer install
        # cannot stop the other three backends from booting, the same reason
        # the codex import is lazy one branch up.
        from litetui.ninfer_backend import NInferBackend
        return NInferBackend(settings)
    if settings.backend != "lmstudio":
        raise BackendError(
            f"That backend is unavailable. Choose one of {', '.join(BACKEND_NAMES)}; "
            "no fallback was used."
        )
    return LMStudioBackend(settings)
