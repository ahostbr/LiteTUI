# C2 shared native activity visibility checkpoint

Native tool lifecycle events now maintain provider-owned activity rows separately
from bg_tasks and its persisted process store. Rows carry native thread/turn/item
identity, command/tool name and bounded sanitized arguments, real elapsed time,
and running state. They have no proc or owner_pid fields. Terminal events remove
only the matching row; transport finish settles interrupted rows through the same
event path. Historical reconciliation explicitly disables this live registry.

The shared footer partition, activity panel, RPC text and /tasks list include these
rows for the selected conversation while the Codex backend is selected. The panel
labels them Codex-owned operations and offers Stop Codex turn, wired to the existing
whole-turn stop action. /tasks kill on a native row explains that scope and never
calls the host process-kill path. Existing host task storage/kill behavior remains.

Validation: 59 combined task-panel/native tool/history/transport tests passed before
the command-list follow-on; the final 3 native-task tests pass, including actual
Textual two-operation panel rendering/button activation, independent settlement,
duplicate start handling, conversation/backend filtering and /tasks kill routing.
New module/tests/plugin Ruff pass. Other changed files have no added diagnostics:
app.py 67, tasks.py 2, task_screens.py 5, native UI/history 0. Diff check passes.

This tracks active native operations, not the lifetime of a child agent after a
spawn operation returns. Delegated-agent lifetime/background control and targeted
per-operation cancellation remain open protocol/implementation gates. Native
process IDs are never guessed. Full C2 and release acceptance remain open.
