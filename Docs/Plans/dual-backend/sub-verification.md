# Sub-Plan WS4: Verification — Instruments, Conditions, Negative Proofs

- **master:** [master.md](master.md) · **phase:** 3 (final gate) · **dependencies:** WS1+WS2+WS3
- **v9 rule:** every criterion names a COMMAND that exits non-zero when violated, states its
  measurement conditions, and has been SEEN to fail once. "My eyes" items are explicit
  `MANUAL:` rows with exactly what Ryan must observe.

## Memory-safety doctrine (Ryan: "don't OOM the system")

- ALL automated verification uses the smallest GGUF on the box (≤1.5 GB, ~0.8B class).
  20GB+ models appear ONLY in the MANUAL pass, one at a time, after `/unload` of the prior.
- `--models-max 2` default; E2E asserts the flag is on the spawned cmdline.
- Boot-loads-nothing is itself a criterion (the 2026 OOM lesson: concurrent test boots
  each loading a model).

## Instruments

### [x] V1 — Mocked suite (runs everywhere, every commit)
- `python tests/run_all.py` → exit 0.
- **Conditions:** clean tree · no llama-server or LM Studio required · all stubs on
  ephemeral ports (bind :0, read back) · **three consecutive runs** all green — one
  favorable run is not a result (the flaky-suite lesson).
- **Negative proof (executed once, then reverted):** break the router stub's `/models/load`
  route → `test_llama_router.py` FAILS. Gate seen failing = gate known live.

### [x] V2 — Real E2E (this box) — `e2e/llama_e2e.py`
Bail=1 (first failure aborts), artifacts under `e2e/artifacts/dual-backend/`. Steps, each a
hard assert:
1. `~/.litesuite/llm/bin/llama-server.exe` exists — else **exit 2** printing
   "llama-server not installed — run LiteSuite's Model Hub wizard." (loud, never a skip).
2. Pick smallest `*.gguf` ≤1.5 GB across the discovery roots — else exit 2 naming the roots scanned.
3. Spawn router via the REAL supervisor (ttyguard path, detached, log file) on an
   ephemeral port with a generated ini → `/health` 200 within 120 s.
4. `GET /models` lists the model; **cmdline contains `--no-models-autoload` and
   `--models-max`** (boot-loads-nothing, OOM guard).
5. `POST /models/load` → status flips loaded; `model_info` returns (window>0, _, True).
6. Stream a completion through the SAME AsyncOpenAI code path the app uses ("Say
   EXACTLY: LITETUI-E2E-OK") → assert marker in the joined stream (outcome, not 200 —
   transport-vs-outcome rule).
7. Apply `{"ctx": 4096}` via `apply_load_settings` → re-read `model_info` window ≤ 4096+64
   (llama clamps; compare against ASKED, the app.py:2507 lesson).
8. `POST /models/unload` → status flips unloaded (VRAM freed by server's own accounting).
8b. Seat-guard cycle (Sentinel finding): re-load the model, then drive
   `seat_guard.suspend`/`resume` THROUGH the backend — suspended = not listed loaded,
   resumed = loaded at the recorded ctx (asked-vs-got compare), breadcrumb file created
   then cleared. On a simulated attached backend: the honest-refusal string, no unload sent.
9. Kill spawned pid; assert process gone; log file non-empty (the instrument that writes
   the log demonstrably ran — instrument-trust audit).
- **Conditions:** this box, GPU idle-ish (no other model resident), run serially, never in
  the mocked suite's process.
- **Negative proof:** run once with the models dir masked (`LITETUI_E2E_MODELS_DIR` pointed
  at an empty tmp) → exits 2 with the roots message. Recorded in the run log.

### [x] V3 — LM Studio backend E2E — `e2e/lms_e2e.py`
1. LM Studio reachable at :1234 — else exit 2 "start LM Studio" (loud).
2. SDK loads the 0.8B with `contextLength=4096` → native `/api/v0/models` shows
   `loaded_context_length≈4096` (SDK path replaces `lms load` — proven by outcome).
3. Same marker-completion stream through the app's client path.
4. SDK `.unload()` → `/api/v0/models` shows not loaded.
- **Conditions:** LM Studio desktop running, small model downloaded in it.

### [x] V4 — Cross-backend integration — `e2e/backend_switch_e2e.py`
Scripted app-level run (Textual pilot): boot llamacpp → 2-turn convo → `/backend lmstudio`
→ 1 more turn → assert one continuous conversation list (4 messages + system), two
different base_urls observed in the transport log.

### [x] V5 — Static gates (already in the suite, must stay green)
- `tests/test_ttyguard.py` (rglob src/**) — no raw subprocess anywhere new.
- `tests/test_plugin_dogfood.py` — re-accretion + plugin counts.
- `tests/test_no_dead_controls`-family — every new Settings field has a reader.
- `git grep -n "localhost:1234\|localhost:7470\|localhost:8088" src/ | grep -v "settings.py\|config.py\|llm_backend.py"` → empty — hosts live in exactly the owned homes (the config.py docstring's rot lesson, now three hosts wide).

## MANUAL checklist (Ryan's eyes — the plan is not done until these are checked)

- [ ] MANUAL: First boot with both engines → picker appears ONCE; choice persists across restarts; Esc re-asks next boot.
- [ ] MANUAL: `/modelcfg` on Qwen3.8 27B side-by-side with the four LM Studio screenshots — every control present / greyed-with-reason / explicit n-a; values round-trip after restart.
- [ ] MANUAL: 27B load at ctx 120064, ngl 65, flash-attn, KV q8_0 → streams; footer shows the LOADED window (not 262144 ceiling); tok/s sane for a 5090.
- [ ] MANUAL: draft-model speculative decoding with the 0.8B as draft → visible tok/s uplift on the 27B.
- [ ] MANUAL: a vision GGUF + its mmproj → paste an image → the model describes it (view_image path).
- [ ] MANUAL: Structured Output with a small JSON schema → reply is valid JSON matching it.
- [ ] MANUAL: `/backend` flip mid-conversation → history visibly intact, header badge flips.
- [ ] MANUAL: While LiteSuite serves :8088 → LiteTUI attaches (says so), refuses load-settings edits with the Model-Hub pointer; stop LiteSuite → `/reconnect` → LiteTUI spawns its own and control returns.
- [ ] MANUAL: GPU memory in Task Manager returns to baseline after `/unload` of the 27B.

## Task list
- [x] T1 — Write V1 negative-proof harness note + execute/revert it (recorded in commit message).
- [x] T2 — `e2e/llama_e2e.py` (V2) + its masked-dir negative proof.
- [x] T3 — `e2e/lms_e2e.py` (V3).
- [x] T4 — `e2e/backend_switch_e2e.py` (V4).
- [x] T5 — Run the full ladder V1→V5 in order, three consecutive V1 runs, record outputs under `e2e/artifacts/dual-backend/`.
- [] T6 — Walk the MANUAL checklist with Ryan; every unchecked box is an open defect, not a note.

## Validation Commands (the gate itself)
- [x] `python tests/run_all.py && python tests/run_all.py && python tests/run_all.py` — exit 0 ×3.
- [x] `python e2e/llama_e2e.py` — exit 0; then with `LITETUI_E2E_MODELS_DIR=<empty>` — exit 2 (negative proof live).
- [x] `python e2e/lms_e2e.py` — exit 0 (LM Studio running).
- [x] `python e2e/backend_switch_e2e.py` — exit 0.
- [] MANUAL checklist above — all boxes.
