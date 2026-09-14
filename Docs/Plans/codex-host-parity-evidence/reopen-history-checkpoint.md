# C3 reopen history refresh checkpoint

Opening a Codex conversation now schedules a separate native-history worker after
rendering the saved transcript. The transport reads `thread/read` with includeTurns
under its existing lock. It does not resume a thread, start a turn, or send user
input. Changed persisted traces are redrawn through the same renderer used by
initial resume; read failures leave the saved view available with a short notice.

Before and after the read, the transport checks conversation object identity,
message count, backend identity and chat activity. A switch, appended message,
backend change or new chat operation discards the pending response. The worker
also checks identity and chat activity before redrawing. Native thread identity
must match. No native reference means no server start/read.

Validation: 39 focused history/app-server/tool UI/compaction tests pass. Fake-server
read tests cover all four races and assert thread/read is the only request. Worker
tests cover changed/unchanged/error/switched results. Scoped Ruff passes; app.py
retains the same 67 pre-existing diagnostics, no additions; diff check passes.
No live provider inference was performed.

Full acceptance remains open: rendered terminal/disk-restart journey, scroll/fold
preservation across refresh, RPC history delivery, legacy unmapped turns and
remaining C3 export/fork/delete cases. This is not release clearance.
