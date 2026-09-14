# C3 rendered and partial history recovery checkpoint

Native agentMessage text from interrupted, failed and in-progress turns is now
recovered with its phase and corresponding non-completed state. This does not
infer tool completion. Repeated partial snapshots remain idempotent.

Replayed tool cards carry their native thread/turn/item identity as local widget
state. A history-triggered redraw captures card expansion and scroll position,
then restores them after layout only if the same conversation remains active and
chat is not running. Ordinary initial resume retains its previous behavior.

Actual LiteTUI Textual run_test at 80x30 exercises a transcript with 15 earlier
messages, an interrupted card, an explicitly expanded card and a nonzero scroll
position. Reconciliation replaces that card with its completed native result;
redraw retains expansion and exact scroll position. Repeated redraw shows one
card. AppServer.start is patched to fail if this offline rendered test attempts
to launch Codex. Three partial-answer state cases also pass.

Validation: 43 focused history/app-server/tool UI/compaction tests pass in 3.10s.
Scoped Ruff passes; app.py has the same 67 baseline diagnostics, no additions.
Diff check passes. No live inference was performed.

Still open: live disk restart, broad terminal resizing and mixed plan/question fold
state, RPC/GUI history delivery, legacy association, and remaining C3 lifecycle
and export gates. This evidence does not clear the full release hold.
