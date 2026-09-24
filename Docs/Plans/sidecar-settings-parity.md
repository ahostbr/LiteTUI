# Sidecar settings parity inventory (implementation gate)

This is an inventory of the existing Textual surface, **not** an assertion that the Rust preview edits settings. Canonical per-field scope and apply timing live in `src/litetui/settings_scope.py`; validation/save/conflicts in `settings_service.py`; live effects in `settings_runtime.py`. The Rust preview currently has only representative controls.

| Section | Fields / surfaces to cover |
|---|---|
| Model | Default/pinned model, context length, backend, LM Studio host/graded-thinking models, Codex native engine, custom API URL/key environment/context, llama executable/host/attach/discovery/model paths and limits/load timeout. Model pickers, loaded-resident state, capability and environment locks. |
| NInfer | Host, executable, artifact, max context/concurrency; RTX 5090 capability gate and next-start semantics. |
| Voice | TTS engine/voice/timeout/Edge voice, STT model/mic/hotkey; test voice, install Edge support, capture hotkey, download voice model. `tts_enabled` is registry/model-listed but not demonstrated as a mounted Textual control. |
| Generation | Thinking level; tool/chat token limits; temperature, top-p, top-k, min-p, repeat/presence/frequency penalties, seed and stop sequences. |
| Agent loop | Subagent and summary models, tool enable/iterations/background threshold, Enter interrupt, policy profile, always-allow/deny/disabled tools, context mode/threshold. |
| Compaction | Auto-compact enable/threshold/wake; maximum tokens, thinking and tool iterations; clear screen and recent-message retention. |
| Capabilities | Seat name; skills enabled/roots; MCP enabled and per-server toggles/inventory. |
| Hooks | Hook inventory, policy, command, runtime envelope, repair, save/reload, sample-event test; inspect `hooks_screen.py` and its persistence separately. |
| Themes | Theme name, custom theme identity/palette tokens; inspect custom-theme editor separately. |
| Interface | Thinking/stop-line/stop-time display, autoscroll, error style, footer visibility/order, dialog style/side, image viewer, sidecar preference. |

## Audit corrections and unproven controls

- The mounted Model router includes `custom_base_url`, `custom_api_key_env`, `custom_context_length`; `settings_ui_model.SETTINGS_SECTIONS["model-router"]` omits them. The name `custom_api_key_env` is redacted from the sidecar snapshot and cannot be edited through its gated patch adapter.
- `tts_enabled`, `llama_load_settings`, `model_infer_overrides`, `plugins_disabled`, `backend_chosen`, `llama_presets`, `custom_themes`, and `mcp_disabled_servers` are in `SETTING_SPECS` but mounted-control status or precise handler mapping is not established by this inventory. `tts_timeout` mounts conditionally. Verify actual Textual controls rather than treating registry/model entries as proof of UI parity.
- `settings_ui_model` section scope labels are presentation metadata, **not** ownership: e.g. model-router and generation budgets disagree with canonical `SETTING_SPECS`. Scope on wire must come from the latter.
- Immediate Voice actions have distinct UI identifiers `#voice-test`, `#voice-install-edge`, `#voice-capture`, `#voice-dl-stt`; audit pressed handlers separately. Search and section toggle are view-only. Hooks/custom themes require a separate inventory of mounted controls and actions.

## Cross-cutting behavior

- Edits are a draft until Save. Cancel discards; dirty close asks Save/Discard/Keep editing. Restore defaults confirms. Search and folding do not write settings.
- Environment-locked and capability-disabled controls must expose effective versus saved values and not persist an unchanged invocation override.
- Use `SettingsService.snapshot` with per-destination revisions and `save_patch` with expected revisions. A conflict must allow reload/decision; a partially successful multi-destination save must not claim atomic success.
- Apply only successfully saved fields through `settings_runtime.apply_saved_result`; distinguish applied, failed and pending restart/reconnect/reload. Saving alone must not load a model or restart an engine.
- Scope and apply timing **must be checked against `SETTING_SPECS`** before a sidecar editor implementation; UI grouping alone is not authoritative. Hooks, custom-theme controls and immediate-action handlers need a second field-by-field audit.

Until this matrix is tested field by field, `/settings` stays Textual. Likewise `/calendar` and `/job` stay Textual until job edits round-trip authoritatively.
