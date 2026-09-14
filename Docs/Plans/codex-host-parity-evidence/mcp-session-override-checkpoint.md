# C8 session-only inventory configuration route

mcp-session-override-live.json records a bounded two-turn live probe against the
installed official app-server. The synthetic server configuration is passed only
as CLI overrides for the owned catalog_probe entry. The isolated config.toml is
unchanged byte-for-byte. Other server entries are not replaced by a whole-table
override. No real user config file is touched.

Between completed turns, the probe closes its native process, replaces only the
owned server overrides with revision 2, restarts, verifies the hook remains trusted
and enabled, and resumes the exact same native thread. Read-only history confirms
the first turn remains present before starting turn two. The second turn executes
new beta and alpha with the changed integer schema. All three synthetic calls are
valid; all three native hook statuses are completed. tools/list sees [1,2]. Native
usage and cached-input counts are retained as evidence, not a promised hit rate.

Acceptance is true only with successful inventory calls, hooks, same thread ID,
prior turn in history, unchanged configuration and cleanup. The process closed and
temporary home was removed. Six offline probe tests pass, including exact TOML
round-trip of spaced Windows paths and limiting overrides to the owned server.
Scoped Ruff and git diff --check pass.

This establishes a configuration-preserving mechanism, not complete host inventory
integration. It used no concurrent native tasks or children. Production must not
restart an engine with live parent/child/background work merely to refresh tools.
The implementation needs an idle boundary, explicit pending-refresh behavior,
native work accounting, session-owned bridge lifecycle, current execution-time
schema checks and existing host authorization/hooks. Process restart is distinct
from thread reset; the latter is neither necessary nor authorized by this test.

Remaining acceptance includes host bridge dispatch, disabled/removed tools, new
installations, overlapping names, multiple loaded conversations, deferred search,
legacy dynamic-tool registrations and packaged clients. C8 remains open.
