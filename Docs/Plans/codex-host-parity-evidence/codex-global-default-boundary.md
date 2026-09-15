# Codex global reasoning default boundary

Task: REL-20260914-CODEX-PARITY

The settings panel already described thinking_level as a default for new Codex
conversations, but LiteTUI._on_settings_saved assigned that value through the
current conversation's write-through setter. Saving a changed global default
therefore replaced the current conversation's chosen mode too.

The save handler now preserves the active Codex conversation's mode and explains
that the changed default applies to new conversations. The discriminator uses
the active backend rather than the edited boot-default backend. Other backends
retain their existing immediate-apply behavior.

The regression exercises the real host save handler for all six stored modes,
checks the actual conversation settings file before and after save, reloads the
global settings file, and verifies the newly born conversation default. The same
matrix checks unchanged local-backend apply behavior. The test explicitly creates
a persisted conversation, so an unchanged absent settings file cannot satisfy the
preservation assertion. A previously skipped tool-context threshold control is
now included in the settings capability assertions because it exists in the UI.

Validation: 55 tests passed across test_codex_settings.py,
test_convo_settings.py and test_settings_controls.py. The changed test file passes
Ruff; app.py retains exactly its 67 baseline findings with no added code/message
findings. git diff --check passes.

This closes the specific global-save overwrite defect. It does not establish
packaged UI acceptance, all-client settings parity, or the remaining native
lifecycle and inventory requirements. No live inference, release, merge or push
was performed for this checkpoint.
