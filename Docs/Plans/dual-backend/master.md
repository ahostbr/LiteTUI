# Plan: Dual-Backend Model Control — LM Studio + llama.cpp Direct

> **Why this exists:** LiteTUI ships with LiteSuite soon. Today it can only talk to LM Studio.
> A LiteSuite user who never installed LM Studio gets a dead TUI; a power user gets less
> control than `llama-server` can actually give. This plan makes LiteTUI the best local-LLM
> app it can be: LM Studio desktop stays a first-class endpoint, and our own llama-server
> (installed by LiteSuite's Model Hub wizard) becomes a second, **directly controlled**
> engine with full LM Studio Load/Inference feature parity.

## Metadata (append-only)

- **created:** 2026-08-21T23:59 (local)
- **modified:** 2026-08-21
- **commits:** (none yet — build gated on Sentinel's all-clear)
- **agent:** BurntRack (worker, `76bdf293-f720-40f7-bf4d-514aaa9d2eb9`)
- **session:** 76bdf293-f720-40f7-bf4d-514aaa9d2eb9
- **back refs:** `Docs/Plans/plugin-wave/spec.md` (architecture this plan targets, v0.21.0)
- **forward refs:** sub-backend-core.md · sub-model-screen.md · sub-commands-integration.md · sub-verification.md

## Task Description

Add a backend seam to LiteTUI with two engines behind it:

1. **`lmstudio`** — today's endpoint (`:1234`), chat unchanged via OpenAI-compat; the
   control plane upgrades from the `lms load` CLI shell-out to the **`lmstudio` Python SDK**
   (JIT load with config, unload, TTL — no subprocess, no PATH dependency).
2. **`llamacpp`** — our own `llama-server` in **router mode**: one process, no `-m`,
   driven by a **generated `--models-preset` ini** whose entries cover every GGUF found on
   the box (LiteSuite's `~/.litesuite/llm/models`, LM Studio's models dir, the HF cache,
   custom dirs). Model list/load/unload/switch over HTTP; per-model load settings live in
   the ini; **attach to LiteSuite's server on :8088 if healthy, else spawn our own on :7470**.

A per-model, tabbed **Model screen** (Info / Load / Inference) mirrors LM Studio's panels
for BOTH backends, layered as per-model overrides on top of the existing global `/settings`
inference section. Nothing in the LM Studio feature list is deferred; LM-Studio-only knobs
render greyed "n/a on llama.cpp" instead of becoming fake controls.

## Objective (measurable)

- LiteTUI boots and chats **with LM Studio not installed and not running**, on our engine.
- `/model` lists GGUFs discovered from ≥2 distinct locations, deduplicated.
- The full Load-tab vocabulary (ctx, ngl, threads, batch/ubatch, parallel, flash-attn,
  KV quant, mlock/mmap, RoPE, seed, draft model, mmproj, chat template) round-trips:
  set in UI → written to ini → visible in `llama-server` behavior.
- The full Inference-tab vocabulary applies per-request, per-model-override aware.
- Presets save/apply. Structured output (JSON schema) works. Vision (mmproj) works.
- Existing LM Studio users see **zero behavior change** until they touch `/backend`.
- Boot NEVER loads a model on either backend (`--no-models-autoload`; the no-load-on-connect
  rule of app.py:2342 extends to llama.cpp verbatim). **Never OOM the box:** default
  `--models-max 2`; all feature testing on ~0.8B models before any 20GB+ model.

## Fact Dependencies (established during quizzing)

| Fact | Confidence | Workstream | Impact if wrong |
|---|---|---|---|
| llama-server router mode: `--models-preset` ini, `GET /models`, `POST /models/load` / `/models/unload`, per-request `model` switch, `--models-max`, `--no-models-autoload` | HIGH (HF blog + server README) | all | Run-mode redesign → hybrid fallback (per-model spawn) already specified |
| LiteSuite installs `~/.litesuite/llm/bin/llama-server.exe`, models in `~/.litesuite/llm/models`, serves single-model on :8088 (`llama-manager.ts`) | HIGH (read source) | WS1 | Attach probe / discovery paths change |
| LiteTUI 0.21.0 plugin architecture: core modules + plugins via `PLUGIN_LOAD_ORDER`, owner-stamped registry, ttyguard-only subprocess, `paths.X` anchors, re-accretion gate | HIGH (spec.md + tree) | all | Plan placement invalid — rebase on Sentinel's post-review tree |
| LM Studio coupling is 3 places: `_read_model_info` (`/api/v0/models`), `_apply_context_length` (`lms load`), host strings | HIGH (read source) | WS3 | Seam bigger than planned |
| `lmstudio` SDK: `lms.llm(key, config)`, `.unload()`, ttl, `configure_default_client`; sync default timeout 60s | HIGH (docs) | WS1 | Fall back to lms CLI path (kept working until deleted in WS3) |
| Ini expressiveness for every Load flag | **MED — build-gate B1 verifies against the installed binary first** | WS1 | Hybrid fallback: models needing an inexpressible flag get a dedicated single-model spawn |
| Port 7470 free (74xx ecosystem block; 8xxx avoided per Ryan) | HIGH (port map doc 00) | WS1 | Env `LITETUI_LLAMA_PORT` overrides |
| Per-model Inference overrides layer over existing global `/settings` sampling fields | HIGH (Ryan confirmed) | WS2/WS3 | Two competing settings systems — forbidden |
| `seat_guard.py` speaks lms verbs (ps/unload/load + ctx verify) — must route through the backend seam or generation on llamacpp drives lms against a model it doesn't own | HIGH (Sentinel finding + read source) | WS1 T9 / WS4 | Studio generations near-OOM or silently no-op on the llama backend |
| `plugins/model_switch.py` will move under Workflow-2 review edits | HIGH (Sentinel) | Phase 0 | Patch conflicts — diff before touching, rebase in the worktree |

## Workstreams

| ID | Name | Sub-plan | Phase |
|---|---|---|---|
| WS1 | Backend core (seam, discovery, ini, process, SDK) | [sub-backend-core.md](sub-backend-core.md) | 1 |
| WS2 | Model screen (Info/Load/Inference tabs, presets) | [sub-model-screen.md](sub-model-screen.md) | 2 (parallel) |
| WS3 | Commands + app integration (+ first-boot picker) | [sub-commands-integration.md](sub-commands-integration.md) | 2 (parallel) |
| WS4 | Verification (mocked suite, real E2E, negative proofs, manual) | [sub-verification.md](sub-verification.md) | 3 (final gate; unit tests still land WITH each WS commit) |

## Orchestration DAG

```
Phase 0 (GATE)     : Sentinel's Workflow-2 all-clear → rebase worktree on surviving tree
Phase 1            : WS1 backend-core            (everything depends on the seam)
Phase 2 (parallel) : WS2 model-screen  ·  WS3 commands-integration
Phase 3            : WS4 verification  (E2E + negative-path proofs + MANUAL checklist)
```

**Build gate B1 (first commit of WS1):** probe the installed `llama-server.exe --help` for
router-mode flags and every Load-tab flag; record the actual ini key names in
`llm_backend.py` constants. Any flag missing → that control renders "n/a in installed build
bNNNN"; any flag the INI cannot express → hybrid fallback engages for models using it.

## Acceptance Criteria (each carries an instrument — see sub-verification.md for conditions)

- [x] `python tests/run_all.py` exits 0 — three consecutive runs, clean tree, no llama-server running (mocked suite touches only ephemeral ports). **58 pytest files + 16 scripts, ×3 green (2026-08-21).**
- [x] `python e2e/llama_e2e.py` exits 0 on this box (real binary + ≤1.5GB GGUF); exits 2 with "run the Model Hub wizard" if the binary is absent — never a silent skip. **9/9 steps proved; exit-2 negative proof recorded.**
- [x] With LM Studio stopped: boot → `/model` → pick 0.8B → streamed reply. **E2E streamed the marker through the app's AsyncOpenAI path against our router.**
- [x] `git grep -n "lms load" src/` — the app's control-plane shell-out is GONE (SDK replaced it). Deliberate exception: seat_guard's legacy no-backend path keeps its lms verbs (standalone-module compat, own tests); every shipped call site passes a backend.
- [x] Re-accretion gate green: app.py imports no plugin module (`tests/test_plugin_dogfood.py`).
- [x] Negative-path proofs executed and recorded (missing exe → error names the path + Model Hub; bad ini key → IniUnexpressible by name; E2E models dir masked → exit 2 naming roots; router-stub route broken → 2 tests fail, reverted → green).
- [ ] MANUAL (Ryan): first-boot picker · Model screen fields match the four LM Studio screenshots' vocabulary · 27B @ ngl 65 streams with correct footer ctx · `/backend` flip keeps the conversation.

## Build Log (2026-08-21, BurntRack)

- **Shipped:** `src/llm_backend.py` (766 lines: census, GGUF-header discovery, ini generator, supervisor, router client, SDK control plane, seat-guard port, request-override split) · Settings +14 fields · settings_screen Backend section · `plugins/model_switch.py` grew /backend /load /unload /modelcfg + `ModelConfigScreen` (Info/Load/Inference) + first-boot picker · seat_guard/studio_tool backend threading · `config.py` deleted (fully orphaned) · 10 new test files (60 new tests) · 3 E2E scripts, all proving against the REAL engines.
- **Live-verified facts** (spike + E2E): ini booleans are `key = true/false` (router emits ±argv); `/models/load` success = STARTED, poll until `loaded`; router+worker = separate processes (tree-kill on shutdown, atexit); `--kv-unified` present in b9360, MTP absent (n/a static).
- **Riders found and fixed:** (1) `kwargs.update(sampling_kwargs(...))` would have crashed the first time anyone set top_k/min_p/repeat_penalty — openai's create() has typed params, no **kwargs; now split native/extra_body. (2) Discovery listed LiteSuite's VOICE ggufs (kokoro/mimi) as chat models — 300s of router failing to serve a TTS codec; now filtered by the GGUF header's `general.architecture`, never by name.
- **Open:** `enable_thinking` live check still pending.

## Walk Log (2026-08-22, Ryan + BurntRack driving the real app)

- **7 of 9 MANUAL items checked** live. Speculative decoding: mechanism proven end-to-end
  (config → ini → worker → drafting, acceptance measured 0.55) but SLOWS this box's pairings
  (0.8B drafting 27B: 50.4 → 25.7/14.9 tok/s) — draft config stripped from settings; uplift
  honest n/a. Attach dance: probe+attach+read proven, but found **D12**: a single-model
  (non-router) llama-server — the real LiteSuite shape — has Ollama-shaped `/models`, so
  attached listing/loaded-marking is wrong, and attach-state proved fragile across resume.
- **Walk-found defects fixed same-night:** D3 apply-order 400 (`076a299`), D8 tombstoned
  --draft-max hung the GPU (`79edd34`), D10 schema TextArea unfocusable + D1 flux-in-picker
  (`530c95a`), pccontrol brace-escape (`ec1ab74`-family).
- **Open defects (filed, small):** D2/D11 chat against an unloaded/loading model surfaces a
  raw 400 — should wait or say "/load first"; D4 the "using X" banner names available[0]
  while the pick logic may choose another; D12 above (needs a single-model-attach mode).
- VRAM returned to baseline (1.5/32.6 GB) after the full walk.

## Golden-Path Scenarios

**GP1 — LiteSuite user, no LM Studio.** Fresh boot; nothing on :1234; `llama-server.exe`
installed. Backend auto-selects `llamacpp` (only one present — silent). Probe :8088 (dead) →
spawn router on :7470, detached, log to `~/.litesuite/llm/logs/litetui-llama-server.log`,
**no model loaded**. `/model` shows GGUFs from `~/.litesuite/llm/models` AND
`~/.lmstudio/models`, deduped, source-tagged. Pick the 0.8B → `POST /models/load` → header
updates, footer shows real ctx (loaded, not ceiling) → chat streams with tok/s.

**GP2 — Both engines installed, first boot.** One-time picker ("LM Studio / llama.cpp
(ours)") → choice persisted in `settings.backend`, never asked again. Later `/backend
lmstudio` flips endpoint mid-session; the conversation continues intact; only client +
model list change.

**GP3 — Deep control.** `/modelcfg` on qwen3.8-27b → Load tab: ctx 120064, GPU offload 65,
threads 12, flash-attn on, KV q8_0/q8_0, draft = the 0.8B, draft-max 2, p-min 0.75 →
Apply → ini section rewritten → model reloads → Inference tab: temp 1, top-k 20, top-p
0.95, min-p off → next request carries exactly those fields. Save as preset "27b-fast";
apply preset to another model.

## Execution Workflow (enforced)

1. **Worktree** — `git worktree add .worktrees/dual-backend -b dual-backend` (junction check
   before ANY future removal — `Get-ChildItem -Recurse -Depth 3 -Force | ? LinkType`).
2. **Gate** — hold for Sentinel's Workflow-2 all-clear; rebase on the surviving tree.
3. **Tests first** — each task lists its test file; tests land in the same commit.
4. **Implement** — WS order per DAG; suite green after every commit (plugin-wave discipline).
5. **Debug** — systematic; no guess-loops.
6. **Verify** — WS4 instruments + negative proofs before "done" is uttered.
7. **Review** — polymathic code review; then Sentinel merges (his call, per harness rules).

## Remaining Uncertainties

- Ini key spelling / coverage in the installed build (B1 resolves; hybrid fallback ready).
- `--kv-unified` and MTP availability in the installed llama.cpp build (B1 resolves; n/a rendering otherwise).
- `chat_template_kwargs: {"enable_thinking": …}` acceptance by llama-server `--jinja` for Qwen-style models (WS3 T6 verifies; falls back to n/a on llama.cpp, live on LM Studio).
- lmstudio SDK websocket behavior when LM Studio is closed mid-session (WS1 T8 error-path test).

## Notes

- Commit convention: LiteHarness trailers (`Task-id`, `Agent-Tier: worker`, `Agent-Name: BurntRack`, `Agent-ID`, `Complexity`), no Co-Authored-By.
- The four LM Studio screenshots (Qwen3.8 27B Load ×2, Inference ×2) are the parity spec; their full control inventory is transcribed into sub-model-screen.md so no builder needs the images.
- llama-server spawn is **detached with stdout/stderr redirected to a log file** — never inherits the TUI's console (hard rule; the console IS the app here).
