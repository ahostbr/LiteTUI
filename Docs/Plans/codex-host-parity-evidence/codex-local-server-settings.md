# Local server controls under Codex

Task: REL-20260914-CODEX-PARITY

Twelve settings remained editable under Codex despite configuring local model
servers: default_context_length, lm_host, lms_load_timeout_s,
lmstudio_graded_thinking_models, llama_executable, llama_host,
llama_attach_hosts, llama_scan_litesuite, llama_scan_lmstudio,
llama_scan_hf_cache, llama_models_dirs and llama_models_max.

The Codex capability map now classifies them as inapplicable to the app-server.
The existing settings builders disable them and explain that their saved values
are retained for local backends. The backend selector and supported host controls
remain editable. No global configuration file is migrated or erased.

Source boundaries: app._apply_context_length returns immediately for remote
backends; thinking_capabilities applies the graded allowlist only to LM Studio;
llm_backend owns local model discovery, router model limits and the LM Studio
SDK load timeout. These settings are not app-server request controls.

Validation: 18 tests passed across test_codex_settings.py and
test_settings_controls.py. The mounted screen tests assert editability for both
Codex and LM Studio and round-trip every local-server value, including nondefault
numbers, a false switch and model/path lists. Scoped Ruff and diff checks pass.

This closes these specific silent no-op controls. It does not establish complete
C9 client parity or close the C2/C8 native lifecycle and inventory gates.
