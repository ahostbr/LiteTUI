# C3 live native-process and disk restart evidence

Installed CLI: codex-cli 0.154.0. Command:

`PYTHONPATH=src python scripts/codex_history_restart_probe.py --live --output Docs/Plans/codex-host-parity-evidence/history-restart-live.json`

The opt-in probe used a temporary Codex home with a private copy of the existing
subscription credentials. One synthetic medium-effort Astra turn called the host
echo tool exactly once and returned an answer. It persisted provider metadata,
closed the original app-server, removed only the synthetic local display trace
to simulate persistence lag, and reloaded the transcript through the production
ConversationRepository reader.

A fresh AppServerTransport/AppServer read native history and reconciled the trace.
Assertions verify both original native item identities, the successful ECHO_OK
tool result and completed assistant answer, no duplicate items, persistence back
to disk and equal subsequent disk reload, and an idempotent second history read.
The recovery process issued zero turn/start and zero thread/resume requests.
The echo tool executed only during the original turn. Both child processes closed;
the temporary home and its credentials were removed in normal cleanup.

Two intentional runs were performed, each one synthetic turn and one host echo:
the first verified identity/disk/idempotence; the second added explicit recovered
tool and assistant content assertions. The saved JSON is the second run, not a
sum. Both exited zero. Evidence stores counts and booleans, not content or IDs.

This proves the live transport/disk recovery route. It does not establish killed
mid-turn recovery, a packaged desktop restart, GUI/RPC history delivery, or all
remaining C3 fork/delete/export and legacy association cases. Release hold remains.
