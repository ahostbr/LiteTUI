# C9 native model catalog checkpoint

OAuthBackend.list_models now obtains available models, visibility, reasoning modes,
default effort, modalities, service tiers and multi-agent version from the installed
official app-server model/list endpoint. It follows pagination and rejects repeated
cursors. The old models_cache.json is supplemental only for context_window and
effective_context_window_percent, which the native Model schema does not expose.
Models remain selectable without that cache; unknown context capacity is not invented.
A failed native catalog is reported instead of silently substituting stale modes.
Saved model inference overrides are not changed by catalog refresh.

Installed 0.154.0 ModelListResponse/ModelListParams schemas were inspected locally.
Live metadata-only model/list evidence is astra-native-catalog.json. It reports
low, medium, high, xhigh, max and ultra, default medium, text/image input, multi-agent
version v2, and priority/Fast service tier. Its description identifies Ultra with
automatic delegation; actual demanding-work delegation is a separate runtime gate.

Validation:

- PYTHONPATH=src `python -m pytest tests/test_codex_catalog.py tests/test_thinking_capabilities.py tests/test_codex_settings.py tests/test_astra_codex_parity.py -q`: 22 passed in 1.92s.
- `python -m pytest tests/test_oauth_ui.py tests/test_codex_catalog.py tests/test_codex_app_server.py tests/test_codex_settings.py tests/test_thinking_capabilities.py -q`: 28 passed in 3.58s.
- Ruff passes oauth_backend.py and test_codex_catalog.py.
- Live metadata-only call through the changed OAuthBackend: six models, all six
  Astra modes, native default medium, saved medium override unchanged, supplemental
  effective context window 258400. App-server closed in finally, exit zero. No
  inference request or Suite runtime launch occurred in this check.

Tests cover stale cached xhigh/default/visibility being overridden by the native
catalog, pagination, no cache, failed catalog, unchanged saved effort and repeated
cursor rejection. Full GUI/settings persistence and packaged acceptance remain open.
This checkpoint does not close all of C9 or grant release clearance.

## Conversation persistence follow-up

Reproduced an actual restore mismatch before changing code: open a Codex conversation
with XHigh, then an older file with thinking_level=medium but no reasoning_effort.
The header became Medium while the per-model request override stayed XHigh. The
regression failed with actual value xhigh versus expected medium.

Restore now uses an older Codex conversation's explicit thinking_level when its
separate reasoning_effort is absent. Default explicitly removes the stale override.
When a Codex-specific saved effort exists it also determines the displayed level,
so an old disagreement between fields cannot show one mode and send another.
Restoring does not rewrite the conversation file. This does not change non-Codex
restore precedence.

Validation: PYTHONPATH=src `python -m pytest tests/test_codex_persistence.py tests/test_convo_settings.py tests/test_thinking_capabilities.py tests/test_codex_settings.py tests/test_codex_catalog.py -q`: **61 passed in 2.09s**.
The tests exercise real app property setters, set_thinking, conversation JSON files
and restore code across all six modes with a conflicting XHigh global default,
older Medium/Default records and disagreeing saved fields. The new test file passes
Ruff. No live provider calls. Graphical/released-client restore journeys remain a
separate acceptance gate.
