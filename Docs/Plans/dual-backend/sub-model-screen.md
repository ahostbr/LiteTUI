# Sub-Plan WS2: Model Screen (Info / Load / Inference)

- **master:** [master.md](master.md) · **phase:** 2 (parallel with WS3) · **dependencies:** WS1
- **Deliverable:** `ModelConfigScreen` — a tabbed, per-model screen mirroring LM Studio's
  panel, driving BOTH backends. Lives INSIDE `src/plugins/model_switch.py` (ruling 6:
  screens move with their owning plugin; HelpScreen/PickerScreen precedent). Opened by WS3's
  `/modelcfg` command and from the `/model` picker footer hint.

## Layering rule (Ryan-confirmed)

The existing global inference section in `/settings` (temperature, top_p, top_k, min_p,
repeat/presence/frequency penalty, seed, stop, max_tokens_*, thinking_level) stays THE
global default. This screen's Inference tab stores **per-model overrides** in
`settings.model_infer_overrides[key]` — an unset field means "inherit global" and renders
the inherited value dimmed with an `(inherited)` tag. No duplicate settings system.

## Visual/structure spec (transcribed from the four LM Studio screenshots — builders never need the images)

Three tabs (Textual `TabbedContent`): **Info · Load · Inference**. Title row: model key +
backend badge. Bottom bar: `[Apply]` `[Save Preset…]` `[Presets ▾]` `[Close]`. Apply on the
Load tab = `backend.apply_load_settings(key, cfg)` (llama: ini rewrite + reload; a loaded
model shows "will reload — evicts resident weights" in the warning hue). Fields follow the
existing SettingsScreen widget idiom (Input + Switch + Select; reuse its CSS classes —
`settings_ui` plugin is the style reference).

### Info tab
| Row | Source |
|---|---|
| Key, file name, size, quant (from name), arch | `ModelRow` + GGUF filename parse |
| Source path(s) — all locations found, precedence-marked | `ModelRow.path` + dedupe extras (WS1 T2) |
| Context ceiling (n_ctx_train) / currently loaded window | `backend.model_info` triple |
| Loaded state + resident location (ours :7470 / attached :8088 / LM Studio) | backend |

### Load tab — full LM Studio vocabulary → llama-server mapping (THE table; dict keys are canon)

| LM Studio control (screenshot) | dict key | llama-server flag / ini key | Widget | n/a rules |
|---|---|---|---|---|
| Context Length (120064, "supports up to 262144") | `ctx` | `-c` / `ctx-size` | Input+slider hint, ceiling shown | both backends (LMS: SDK contextLength) |
| GPU Offload (65 layers) | `ngl` | `-ngl` / `n-gpu-layers` | Input, max = layer count if known | LMS: SDK gpu config |
| CPU Thread Pool Size (12) | `threads` | `-t` / `threads` | Input | LMS: greyed "LM Studio manages" |
| Evaluation Batch Size (2048) | `batch` | `-b` / `batch-size` | Input | LMS greyed |
| Physical Batch Size (512) | `ubatch` | `-ub` / `ubatch-size` | Input | LMS greyed |
| Max Concurrent Predictions (4) | `parallel` | `-np` / `parallel` | Input | LMS greyed |
| Unified KV Cache (exp.) | `kv_unified` | `--kv-unified` | Switch | greyed unless flag in T1 census |
| Context Checkpoints (32) | — | none | Static "n/a on llama.cpp" | LMS-only concept |
| Reasoning Budget Message | — | none | Static "n/a v1" | LMS-only |
| RoPE Frequency Base / Scale (Auto) | `rope_base` / `rope_scale` | `--rope-freq-base/scale` | Input, empty=Auto(omit) | LMS greyed |
| Offload KV Cache to GPU (on) | `kv_offload` | write `--no-kv-offload` when OFF | Switch | LMS greyed |
| Keep Model in Memory (on) | `mlock` | `--mlock` | Switch | LMS greyed |
| Try mmap() (on) | `mmap` | write `--no-mmap` when OFF | Switch | LMS greyed |
| Seed (Random) | `seed` | `--seed` (omit = random) | Input, empty=random | both |
| Speculative Decoding (MTP dropdown) | `draft_model` | `-md <path>` (draft-file mode) | Select from discovered small GGUFs + "off" | MTP itself: greyed unless census finds it |
| — Max draft tokens (2) | `draft_max` | `--draft-max` | Input | with draft_model |
| — Min draft tokens (0) | `draft_min` | `--draft-min` | Input | with draft_model |
| — Draft probability (0.75) | `draft_p_min` | `--draft-p-min` | Input | with draft_model |
| Chat Template | `chat_template_file` | `--chat-template-file` + `--jinja` | Input (path), empty=model default | LMS greyed |
| Flash Attention (on) | `flash_attn` | `--flash-attn` on/off/auto | Select | LMS greyed |
| K Cache Quantization Type (F16) | `cache_k` | `--cache-type-k` | Select: f16 q8_0 q5_1 q5_0 q4_1 q4_0 | LMS greyed |
| V Cache Quantization Type (F16) | `cache_v` | `--cache-type-v` | Select: same | LMS greyed |
| Vision projector (LiteTUI addition) | `mmproj` | `--mmproj <path>` | Select from discovered `*mmproj*` files | enables image turns via existing view_image path |

Greyed = visible, value shown when known, not editable, one-line reason — a control that
exists but explains itself beats a control that silently vanishes (the tool-gate doctrine,
same house style).

### Inference tab — per-model overrides (BOTH backends; unset = inherit global)

| LM Studio control | override key | Wire (llama) | Wire (LM Studio) |
|---|---|---|---|
| Preset select / Save Preset As… | — | `settings.llama_presets` (shared store, both backends) | same |
| Reasoning Effort (Extra High) | `thinking_level` | existing reasoning_effort request field | same (existing path) |
| Enable Thinking (on) | `enable_thinking` | `chat_template_kwargs:{"enable_thinking":…}` via extra_body — census-gated | n/a (LMS UI owns it) |
| Preserve Thinking (on) | `preserve_thinking` | LiteTUI-side: keep reasoning content of PRIOR assistant turns in history instead of last-only (WS3 T6 wires; default = today's behavior) | same |
| Reasoning Budget | — | n/a v1 (Static) | n/a |
| Temperature (1) | `temperature` | request field | request field |
| Limit Response Length | `max_tokens` | request field (else global max_tokens_*) | same |
| Context Overflow (Truncate Middle) | — | Static: "LiteTUI autocompacts instead (/settings)" — our mechanism is strictly richer | same |
| Stop Strings | `stop` | request field | same |
| Top K Sampling (20) | `top_k` | extra_body top_k | top_k |
| Repeat Penalty (off) | `repeat_penalty` | extra_body repeat_penalty | same |
| Presence Penalty (off) | `presence_penalty` | request field | same |
| Top P Sampling (0.95) | `top_p` | request field | same |
| Min P Sampling (off) | `min_p` | extra_body min_p | same |
| Structured Output (off) | `json_schema` (str) | response_format json_schema | response_format |

## Tasks

### [x] T1 — Screen skeleton + Info tab
- `ModelConfigScreen(Screen[None])` in `plugins/model_switch.py`; constructor
  `(app, key: str)`; reads via `app.backend` + `app.settings` only (no new app fields).
  BINDINGS: escape=close. Reuse SettingsScreen CSS classes; no new stylesheet.
- Test `tests/test_model_screen.py::test_info_tab`: pilot-run app (existing harness
  pattern), open screen for a fake row, assert source paths + ceiling rendered.

### [x] T2 — Load tab
- Build rows from THE table above; value = `settings.llama_load_settings[key]` merged over
  defaults; greying driven by `backend.name` + `SUPPORTED_FLAGS` census (WS1 T1).
- Apply → `app.run_worker(backend.apply_load_settings(...))`, in-band progress line,
  refetch ctx window after (existing `_fetch_ctx_window` semantics via WS3 seam).
- Tests: dict round-trip (set 3 fields → Apply → settings dict holds exactly those; unset
  field ABSENT, not zero); greyed-on-lmstudio matrix; census-gated kv_unified.

### [x] T3 — Inference tab
- Rows from the override table; `(inherited)` dimming for unset; explicit "clear override"
  affordance per field (sets back to absent).
- Tests: inherit-vs-override rendering; a cleared override leaves no key behind.

### [x] T4 — Presets
- `[Save Preset…]` → name input modal → snapshot current Load+Inference dicts into
  `settings.llama_presets[name]`. `[Presets ▾]` → PickerScreen (core widget) of names →
  apply = deep-merge preset into this model's two dicts + refresh tabs (Load changes take
  effect on next Apply — stated in-line, no silent reload).
- Tests: save → apply to a second model → dicts equal; overwrite prompt on existing name.

### [x] T5 — Structured output editor
- `json_schema` field opens a multiline editor modal (existing TextArea idiom); on save,
  `json.loads` validation — invalid JSON refuses with the parse error in-band.
- Test: invalid schema never lands in settings.

## Golden path (this WS)
Open `/modelcfg` on the 0.8B (llamacpp) → Load: ctx 8192, ngl 99, cache_k q8_0 → Apply →
`llama_load_settings["<key>"] == {"ctx": 8192, "ngl": 99, "cache_k": "q8_0"}` and the ini
section shows exactly those three lines → Inference: temperature 0.4 → next request's
payload carries 0.4 while another model still inherits the global. Save preset "tiny-fast";
apply it to a second model; both dicts match.

## Validation Commands
- [x] `python -m pytest tests/test_model_screen.py -x -q` — exits 0. *Conditions: mocked backend, no network.*
- [x] `python tests/run_all.py` — green, 3 consecutive runs.
- [] Negative proof: feed the Load tab an unknown dict key via settings.json by hand → screen renders it under "unknown keys (kept verbatim)" and Apply preserves it — forward-compat proven, silent data loss impossible.
- [] MANUAL (Ryan): side-by-side with the four screenshots — every LM Studio control present, live, greyed-with-reason, or explicit "n/a" static. No control silently missing.
