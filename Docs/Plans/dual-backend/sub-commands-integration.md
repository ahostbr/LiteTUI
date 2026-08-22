# Sub-Plan WS3: Commands + App Integration

- **master:** [master.md](master.md) · **phase:** 2 (parallel with WS2) · **dependencies:** WS1
- **Deliverable:** the seam in `app.py` (host-side, minimal), extended
  `plugins/model_switch.py` commands, the first-boot picker, request-override wiring.

## Rules in force
- app.py is a HOST: it may hold `self.backend` and delegate, but imports NO plugin module
  (re-accretion gate `tests/test_plugin_dogfood.py`). `llm_backend` is a core module —
  importing it from app.py is legal (peer of `harness.py`).
- Command bodies live in the plugin; `handler(app, name, arg)` signature; token collisions
  raise at load (registry doctrine).
- Boot loads NO model, on either backend (app.py:2342 comment is law; extended verbatim).

## Tasks

### [x] T1 — The seam (app.py, host)
- `__init__`: replace the inline `AsyncOpenAI(base_url=f"{settings.lm_host}/v1")`
  (app.py:1364) with:
  ```python
  self.backend = llm_backend.make_backend(self.settings)
  self.client = AsyncOpenAI(base_url=self.backend.base_url(), api_key="litetui")
  ```
- `_connect` (app.py:2317): first `await self.backend.ensure_running()` (its return line —
  "attached :8088" / "spawned pid N" — goes to `_system`); then list via
  `self.backend.list_models()` instead of `self.client.models.list()` (rows carry source
  tags for the picker); default-model/pin logic UNCHANGED. Connection errors name
  `self.backend.host()` — `settings.lm_host` hardcoding in the message dies (the 2364 lesson).
- `_read_model_info` / `_fetch_ctx_window`: body delegates to
  `await self.backend.model_info(self.model_id)` — the (window, type, loaded) triple and
  the footer's ceiling-vs-window rendering stay byte-identical.
- `_apply_context_length`: delegates to `backend.load(key, ctx=want)` (LM Studio: SDK JIT
  load — the `lms load` ttyguard block is DELETED here); llama: merge `{"ctx": want}` into
  `llama_load_settings[key]` + `apply_load_settings`. The force/compare-against-ASKED
  doctrine (app.py:2496-2533) is preserved in the seam, not re-derived.
- Test: existing suites (`test_tools_registered`, connect/ctx tests) pass UNEDITED against
  a mocked backend — that is the proof the seam is behavior-neutral.

### [x] T2 — /backend command (plugin)
- `ctx.command(("/backend",), _cmd_backend, palette="Switch backend", help="lmstudio | llamacpp (/backend)")`.
- Bare `/backend` → PickerScreen with both rows + live health markers ("● running" /
  "○ not detected"); arg form `/backend llamacpp` for muscle memory.
- Switch = `settings.backend = choice; settings.save()`; rebuild `app.backend` +
  `app.client`; `app._connect()`. **The conversation is NOT touched** — history survives,
  only endpoint + model list change (Ryan-confirmed).
- Test `tests/test_backend_switch.py`: flip mid-conversation → `len(app.conversation)`
  unchanged, client base_url changed, header shows new backend.

### [x] T3 — /load, /unload, /modelcfg (plugin)
- `/load [model]` → explicit load (picker when bare); `/unload [model]` → frees VRAM
  (llama: router unload; lmstudio: SDK unload — new capability); `/modelcfg [model]` →
  pushes WS2's `ModelConfigScreen` (default: current model).
- On the attached-:8088 read-only case, surface the supervisor's refusal string in-band.
- `/model` picker rows gain the source tag + loaded marker:
  `"▸ qwen3-0.6b  · litesuite · loaded"` (extends model_switch.py:40-47 rows; picker API unchanged).
- Test: command → backend method called with right key; refusal path rendered.

### [x] T4 — Request overrides at the call site
- The `_stream` request builder merges `self.backend.request_overrides(self.model_id)`
  into the completion kwargs (extra_body included) — global-vs-per-model precedence is
  WS1 T7's; the call site only merges.
- Test: streamed request captured from a mock transport carries an override temperature
  while a model without overrides sends the global.

### [x] T5 — First-boot picker (plugin activate hook)
- In model_switch's `activate(app)`: if `not settings.backend_chosen` AND both engines
  detected (LM Studio: TCP probe `settings.lm_host`; llama: `LLAMA_EXE.exists()` OR any
  attach host healthy) → push PickerScreen("Which engine should LiteTUI use?"); persist
  `backend` + `backend_chosen=True`. Exactly one engine detected → silently select it,
  set `backend_chosen=True`. Neither → keep default, leave `backend_chosen` False (ask
  when one appears). Esc = keep current default, do NOT set chosen (ask again next boot).
- Test `tests/test_first_boot.py`: 4-state matrix (both/only-lms/only-llama/neither) ×
  picker shown/silent/skipped; Esc leaves backend_chosen False.

### [x] T6 — Thinking controls on llama
- Census-gated (`--jinja` present): when `enable_thinking` override is set, send
  `extra_body={"chat_template_kwargs": {"enable_thinking": bool}}`; `preserve_thinking`
  switches the history builder between last-turn-only reasoning (today) and all-turns.
  Verified live against the 0.8B Qwen in E2E (thinking tags appear/disappear).
- Test: payload assertion both ways; history builder golden test.

### [x] T6b — Studio plugin wires seat_guard through the backend
- `plugins/studio.py`'s suspend-between-completions call sites pass `app.backend` into the
  refactored `seat_guard` (WS1 T9). No verb strings in the plugin; the honest-refusal
  string from attached mode is surfaced in the tool result verbatim.
- **Pre-patch rule (Sentinel):** `plugins/model_switch.py` and `plugins/studio.py` will
  have moved under Workflow-2 review edits — `git diff` them against the rebased tree
  BEFORE patching; command bodies may have new homes.
- Test: studio tool path invokes backend.unload/load (mocked), never a raw `lms` argv.

### [x] T7 — Header/footer
- Header subtitle gains the backend: `"llama.cpp · qwen3-0.6b"` / `"LM Studio · …"`
  (`_update_header`). Footer untouched — the model_info triple already drives it.
- Test: header string per backend.

## Golden path (this WS)
Boot with both engines present, `backend_chosen=False` → picker → choose llama.cpp →
"spawned pid N — no model loaded" → `/model` → rows tagged by source → pick 0.8B → loads,
header "llama.cpp · qwen3-0.6b", footer real window → chat streams → `/backend lmstudio`
→ same conversation continues on :1234 → `/backend llamacpp` → back, still one history.

## Validation Commands
- [x] `python -m pytest tests/test_backend_switch.py tests/test_first_boot.py -x -q` + the T1/T3/T4/T6/T7 files — exit 0. *Conditions: mocked backends, ephemeral ports, no real engines.*
- [x] `python tests/run_all.py` — green ×3 consecutive; `test_tools_registered.py` and the connect/ctx suites UNEDITED.
- [x] `git grep -n "lms load" src/` → nothing outside comments/CHANGELOG — the shell-out is deleted, not orphaned.
- [x] `python -m pytest tests/test_plugin_dogfood.py -x -q` — re-accretion gate still green after the app.py edits.
- [] Negative proof: point `LITETUI_LLAMA_HOST` at a dead port with no exe present → boot survives, `_system` error names THAT host, lmstudio backend still reachable via /backend.
