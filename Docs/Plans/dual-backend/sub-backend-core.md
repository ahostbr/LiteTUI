# Sub-Plan WS1: Backend Core

- **master:** [master.md](master.md) · **phase:** 1 · **dependencies:** none (after Phase-0 gate)
- **Deliverable:** `src/llm_backend.py` (new core module, peer of `harness.py`) + new `Settings`
  fields + mocked test suite. No UI, no commands (WS2/WS3 consume this).

## Hard rules in force

- Subprocess ONLY via `ttyguard.run` / `ttyguard.popen` (`tests/test_ttyguard.py` rglobs `src/**` — a raw `subprocess` import fails the suite).
- This module NEVER imports `app` (re-accretion gate). It receives what it needs via constructor args.
- Path anchors via `paths.X` (`import paths`, module-import style — a from-import copies the binding).
- `None` in Settings = "not configured, omit" (settings.py doctrine); meaningful zeros stay distinct.

## New Settings fields (central dataclass, `src/settings.py` — every field MUST gain a reader; `test_no_dead_controls` enforces)

```python
# ── Backend selection ────────────────────────────────────────────────
#: "lmstudio" | "llamacpp". Env LITETUI_BACKEND wins (add to _ENV_MAP).
backend: str = "lmstudio"
#: First-boot picker shown exactly once, and only when BOTH engines are detected.
backend_chosen: bool = False

# ── llama.cpp endpoint ───────────────────────────────────────────────
#: OUR router instance. Env LITETUI_LLAMA_HOST. 7470 chosen from the 74xx
#: ecosystem block (8xxx avoided — Ryan 2026-08-21); port map doc 00 shows 7470 free.
llama_host: str = "http://localhost:7470"
#: Probed IN ORDER before spawning; a healthy answer = attach, never spawn.
#: Default is LiteSuite's single-model server.
llama_attach_hosts: list[str] = field(default_factory=lambda: ["http://localhost:8088"])
#: Discovery scan roots (all optional, all on by default where they exist).
llama_scan_litesuite: bool = True     # ~/.litesuite/llm/models
llama_scan_lmstudio: bool = True      # ~/.lmstudio/models + legacy ~/.cache/lm-studio/models
llama_scan_hf_cache: bool = True      # $HF_HOME or ~/.cache/huggingface/hub → **/snapshots/**/*.gguf
llama_models_dirs: list[str] = field(default_factory=list)   # extra custom roots
#: --models-max. 2, not upstream's 4: two 20GB models already fill a 5090.
llama_models_max: int = 2
#: Per-model Load-tab overrides: {model_key: {"ctx": 120064, "ngl": 65, ...}}
#: Key vocabulary = the mapping table in sub-model-screen.md, EXACTLY.
llama_load_settings: dict[str, dict] = field(default_factory=dict)
#: Per-model Inference-tab overrides, BOTH backends: {model_key: {"temperature": 1.0, ...}}
model_infer_overrides: dict[str, dict] = field(default_factory=dict)
#: Named presets: {name: {"load": {...}, "inference": {...}}}
llama_presets: dict[str, dict] = field(default_factory=dict)
# ── LM Studio SDK ────────────────────────────────────────────────────
#: lmstudio-python sync API timeout for model loads (default 60s is too short for 20GB).
lms_load_timeout_s: int = 600
```

`_ENV_MAP` additions: `"backend": "LITETUI_BACKEND"`, `"llama_host": "LITETUI_LLAMA_HOST"`.

## Dependency

`pyproject.toml`: add `lmstudio` (the official SDK, PyPI). Chat stays on the existing
`openai` `AsyncOpenAI` client for BOTH backends — the SDK is control-plane only.

## Module contract (`src/llm_backend.py`)

```python
@dataclass(frozen=True)
class ModelRow:
    key: str            # stable id shown in /model and used as dict key everywhere
    path: str | None    # absolute GGUF path (llamacpp) / None (lmstudio)
    source: str         # "litesuite" | "lmstudio-dir" | "hf-cache" | "custom" | "server"
    loaded: bool

class Backend(Protocol):
    name: str                                   # "lmstudio" | "llamacpp"
    def base_url(self) -> str: ...              # OpenAI-compat /v1 for AsyncOpenAI
    def host(self) -> str: ...                  # for error messages — never hardcode a host string
    async def ensure_running(self) -> str: ...  # "ok" | "attached <host>" | "spawned pid <n>" | raises BackendError
    async def list_models(self) -> list[ModelRow]: ...
    async def model_info(self, key) -> tuple[int | None, str | None, bool] | None: ...
                                                # (window, type, loaded) — EXACT semantics of
                                                # app._read_model_info: loaded_len when loaded,
                                                # ceiling+False when merely installed (app.py:2570)
    async def load(self, key, *, ctx: int | None = None) -> None: ...
    async def unload(self, key) -> None: ...
    async def apply_load_settings(self, key, cfg: dict) -> None: ...   # full Load-tab dict
    def request_overrides(self, key) -> dict: ...   # extra_body / params for chat requests
def make_backend(settings) -> Backend: ...          # the one factory app.py calls
```

## Tasks

### [x] T1 — Flag census (build gate B1) — FIRST COMMIT
- Run `ttyguard.run([str(LLAMA_EXE), "--help"], timeout=30)` against
  `~/.litesuite/llm/bin/llama-server.exe`; parse which of these exist: `--models-preset`,
  `--models-dir`, `--models-max`, `--no-models-autoload`, `--kv-unified`, `--flash-attn`,
  `--cache-type-k`, `--rope-freq-base`, `--mmproj`, `-md`, `--jinja`, `--chat-template-file`,
  `--context-shift`.
- Emit `SUPPORTED_FLAGS: frozenset[str]` + `INI_KEYS: dict[str, str]` constants; a control
  whose flag is absent renders "n/a in installed build <version from bin/.build-info>".
- **Router mode absent entirely** (old binary): `make_backend` raises
  `BackendError("llama-server bNNNN has no router mode — update the engine in LiteSuite's Model Hub")`.
- Test `tests/test_llama_flags.py`: parse a captured `--help` fixture; assert census output.

### [x] T2 — Discovery scanner
- `scan_models(settings) -> list[ModelRow]`. Roots: `Path.home()/".litesuite/llm/models"`;
  `Path.home()/".lmstudio/models"` and `Path.home()/".cache/lm-studio/models"`;
  `os.environ.get("HF_HOME", Path.home()/".cache/huggingface")/"hub"` matching
  `models--*/snapshots/*/**/*.gguf`; each of `settings.llama_models_dirs`.
- Skip: files < 10 MB, `*mmproj*` files (they are projectors, offered in the Load tab's
  mmproj picker instead), split shards beyond the first (`*-00002-of-*.gguf`).
- **Key derivation:** file stem, lowercased; on stem collision from different content,
  suffix `~2`. **Dedupe:** same (stem, size) in two roots → ONE row, precedence
  litesuite > lmstudio-dir > hf-cache > custom; keep all source paths on the row for the Info tab.
- Test `tests/test_llama_discovery.py`: tmp-dir tree with a dupe, a shard set, an mmproj,
  a sub-10MB stub → exact expected rows.

### [x] T3 — Ini generator
- `write_preset_ini(rows, settings, path) -> Path` under a new `paths.LLAMA_DIR = ROOT/".llama"`
  (add to `paths.py`; gitignore it): `litetui-models.ini` + `litetui-llama-server.log`.
- One `[<key>]` section per row: `model = <abs path>` + every key present in that model's
  `settings.llama_load_settings[key]`, translated through `INI_KEYS`, inverted-flag handling
  for kv_offload/mmap (`--no-kv-offload` / `--no-mmap` written only when the toggle is OFF).
- Deterministic output (sorted sections, sorted keys) — the test diffs a golden file.
- Test `tests/test_llama_ini.py`: golden-file comparison; a cfg key with no supported flag
  raises `IniUnexpressible(key)` — NEVER silently dropped (hybrid fallback consumes this).

### [x] T4 — Process supervisor (LlamaCppBackend.ensure_running)
- Probe order: `settings.llama_host` `/health` (already ours, running) → each
  `llama_attach_hosts` `/health` (attach; remember attached host for base_url) → spawn.
- Spawn: `ttyguard.popen([exe, "--models-preset", ini, "--host", "127.0.0.1", "--port", port,
  "--models-max", n, "--no-models-autoload"], stdout=logfile, stderr=logfile, detached)` —
  cwd = bin dir (DLLs), never inherits the TUI console. Readiness = `/health` 200 within
  120s, polled at 1s; on timeout read the log tail (last 5 lines) into the error.
- **Attached mode is read-mostly:** against LiteSuite's single-model :8088 server,
  load/unload/apply are refused with "LiteSuite owns this server — switch models in the
  Model Hub, or stop it and I'll run my own" (its process serves ONE fixed model).
- Hybrid fallback: a model whose cfg raised `IniUnexpressible` gets
  `spawn_dedicated(key) -> port` — single-model spawn with FULL argv (LiteSuite's
  `buildServerArgs` vocabulary, llama-manager.ts:807-847); backend routes that key's
  base_url to the dedicated port. Owner tracking so `/unload` kills it.
- Shutdown: `atexit` + app exit kill only processes WE spawned (never an attached server).
- Test `tests/test_llama_supervisor.py`: fake `/health` HTTP stub on an ephemeral port —
  attach beats spawn; spawn cmdline assembled correctly (popen monkeypatched); dead host +
  missing exe → `BackendError` naming the exe path.

### [x] T5 — Router client (list/load/unload/info)
- `GET {host}/models` → rows (server truth) merged with T2 scan (disk truth): a model the
  server doesn't know yet is still offered (ini regenerates + `POST /models/load` reloads config).
- `load`: `POST /models/load {"model": key}`; `unload`: `POST /models/unload`; both surface
  the server's error body verbatim in-band (app._system), never swallowed.
- `model_info(key)`: from `/models` status: loaded → (n_ctx from instance props, "llm"/"vlm"
  when mmproj configured, True); not loaded → (GGUF n_ctx_train ceiling if cheaply available
  from `/models` metadata else None, type, False). Semantics MUST match app.py:2570's
  ceiling-vs-window contract — the footer's "max" tag depends on it.
- `apply_load_settings`: update `settings.llama_load_settings[key]` → rewrite ini →
  `POST /models/unload` + `/models/load` (reload picks up the new section). Never touches
  other loaded models.
- Test `tests/test_llama_router.py`: HTTP stub asserting exact request bodies + the
  unload/load sequence on apply.

### [x] T6 — LMStudioBackend (SDK control plane)
- `import lmstudio as lms` lazily INSIDE methods (a missing dep must not kill boot —
  plugin-failure doctrine; surface "pip install lmstudio" in-band).
- Client: `lms.configure_default_client(host_without_scheme)` from `settings.lm_host`;
  `lms.set_sync_api_timeout(settings.lms_load_timeout_s)`. Every SDK call via
  `asyncio.to_thread` (sync API; the app is Textual-async).
- `load(key, ctx=n)` → `lms.llm(key, config={"contextLength": n})` (replaces the
  `lms load --context-length` shell-out VERBATIM in behavior: explicit act only).
- `unload(key)` → handle from `lms.llm(key)` then `.unload()` — capability LiteTUI never had.
- `list_models` / `model_info`: keep the existing native REST `GET {lm_host}/api/v0/models`
  logic (moved here from app.py, byte-for-byte semantics — it already encodes the
  loaded-vs-ceiling lesson).
- `apply_load_settings`: SDK-mappable subset (contextLength, gpu offload, ttl); the rest
  raises `NotSupported("LM Studio manages this in its own UI")` which the screen renders
  as the greyed state.
- Test `tests/test_lms_backend.py`: `lmstudio` module monkeypatched; assert config dict,
  to_thread usage, absent-dep error path.

### [x] T7 — Request overrides
- `request_overrides(key)`: merge global Settings sampling fields (existing: temperature,
  top_p, top_k, min_p, repeat_penalty, presence_penalty, frequency_penalty, seed, stop)
  with `model_infer_overrides[key]` (override wins, None = omit). llama extras ride
  `extra_body` (min_p, repeat_penalty, top_k are llama-server native fields);
  structured output → `response_format={"type":"json_schema","json_schema":{...}}`.
- Consumed by WS3 T4 (the `_stream` call site).
- Test `tests/test_request_overrides.py`: precedence matrix (global-only / override-only /
  both / explicit None).

### [x] T8 — Error paths
- Every network failure raises `BackendError(msg_naming(self.host()))` — the app.py:2364
  lesson (never blame a host we didn't try) is enforced IN the backend, tested per backend.
- SDK websocket death mid-session (LM Studio quit): next control call surfaces
  "LM Studio is not running at <host>" — not a stack trace.

### [x] T9 — Seat-guard port (Sentinel finding, 2026-08-21)
- `src/seat_guard.py` (studio tool's suspend-between-completions) speaks raw lms verbs
  (`lms ps --json` / `unload` / `load` + ctx verify). Refactor it to take a `backend`
  parameter (the studio plugin passes `app.backend`); its breadcrumb file, retry loop,
  ctx-verify-after-resume, and safety-gate SHAPE stay exactly as written — only the verbs
  route through the protocol:
  - `record(key)` → `backend.seat_snapshot(key) -> dict | None` (identifier, context,
    parallel, status, queued) — new protocol method.
  - `suspend(rec)` → `backend.unload(key)`; `resume(rec)` → `backend.load(key, ctx=…)` +
    re-read snapshot; wrong-context detection unchanged (compare against ASKED).
- **LMStudioBackend.seat_snapshot:** keep ONE narrow read-only `lms ps --json` via
  ttyguard (it is the only source of `status`+`queued`, the do-not-kill-other-seats gate)
  UNLESS the SDK exposes equivalent status at build time — check first, prefer SDK.
  The "delete `lms load`" criterion is unchanged; `lms ps` read-only may survive.
- **LlamaCppBackend:** router-owned → native support (`/models` status busy ⇒ refuse;
  unload/load = suspend/resume); dedicated spawn → stop/respawn via the supervisor;
  attached (:8088, LiteSuite's single-model server) → honest refusal:
  `"suspend unsupported: LiteSuite owns this server"` — the studio tool surfaces it and
  proceeds WITHOUT suspending only if the generation fits, per seat_guard's existing
  caller contract.
- Test `tests/test_seat_guard_backends.py`: suspend/resume cycle against the router stub;
  refusal string in attached mode; LMS path still satisfies the existing seat_guard tests.

## Golden path (this WS alone, mocked)
`make_backend(settings(backend="llamacpp"))` → `ensure_running()` against stub = "attached" →
`list_models()` merges stub+scan → `apply_load_settings("m", {"ctx": 8192})` rewrites ini +
replays unload/load → `request_overrides("m")` carries the override temp.

## Validation Commands
- [x] `python -m pytest tests/test_llama_flags.py tests/test_llama_discovery.py tests/test_llama_ini.py tests/test_llama_supervisor.py tests/test_llama_router.py tests/test_lms_backend.py tests/test_request_overrides.py -x -q` — exits 0. *Conditions: clean tree, no real llama-server running, ephemeral ports only.*
- [x] `python tests/run_all.py` — whole suite still green (no regression), 3 consecutive runs.
- [] Negative proof: rename the stub's `/health` route → supervisor test FAILS (gate seen failing once, then restored).
- [x] `git grep -n "subprocess" src/llm_backend.py` → empty (ttyguard only).
