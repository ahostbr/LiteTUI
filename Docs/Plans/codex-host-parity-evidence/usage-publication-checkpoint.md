# C10 native usage publication checkpoint

The shared RPC projection previously allowed saved provider context/window fields
to overwrite its current context value and loaded-window guard. The regression
test reproduced 999 being emitted instead of the current 1234. These two fields
are now owned by the current context projection; cache and spend fields still pass
through independently.

Normal native turns and manual native compaction now share `_record_native_usage`.
It installs the native window before changing the reactive context value, and
explicitly publishes accounting when occupancy is unchanged (which otherwise
does not trigger the reactive watcher). Thus new cumulative/turn/cache counters
are not suppressed just because the latest request occupies the same context.

Validation: 31 focused tests passed across test_codex_app_server.py,
test_codex_compaction_ui.py, test_codex_usage.py, test_usage_on_the_wire.py and
test_cache_usage.py. The new equal-occupancy regression checks the production
publication helper with a changed window and new cache/thread totals. These are
offline tests; no model processes or provider calls were launched.

Ruff passes codex_app_server.py and test_usage_on_the_wire.py. app.py retains the
same 67 diagnostics as HEAD f8ac93f when compared by code/message; none added or
removed. `git diff --check` passes.

This does not establish full C10 or all-client acceptance. Unknown-context clearing
in consumers, resumed/compacted usage, and live packaged client displays remain
open. Native timing suppression remains subject to the full UI journey checks.
