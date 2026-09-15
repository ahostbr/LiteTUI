# C2/C8 background lifecycle probe remains inconclusive

The new opt-in probe asks one Astra/medium turn to execute one exact synthetic
Python command with a short yield. The script prints markers, sleeps twenty seconds
and exits. Its trusted gate enforces exact command text and a single execution;
other commands and delegation are denied. The process is owned by the isolated
app-server and finally-close cleanup; no unverified PID or external process is used.

Three distinct outcomes are preserved:

- native-background-readiness-live.json: command text matched but the gate expected
  exec_command rather than the native hook's canonical Bash name, so it refused.
- native-background-readiness-canonical-live.json: canonical shell gate allowed the
  command, but no pending native command was observed after turn completion.
- native-background-readiness-diagnostic-live.json: the trusted hook completed;
  no commandExecution start/completion items were observed during the turn. No
  long-lived command was established. The reason remains undiagnosed.

All runs have accepted=false. An observed-idle readiness result in these runs is
not evidence about active background work, because the intended workload was not
established. Do not enable automatic inventory restart on this probe evidence.
All app-server processes closed and temporary homes were removed. No delegation,
Suite/GUI launch, real project writes or raw command output logging occurred.

Offline review found and fixed a separate tracker bug: a later null processId must
not overwrite an earlier known native PTY identity. A subsequent exit event for
that process can now settle the original command, while another thread's matching
opaque string cannot. The regression includes duplicate start/completion with null
processId. Thirty-nine tests pass across runtime, app-server and inventory; scoped
Ruff passes. The exact generated gate also has an offline single-command/negative
input test including the canonical Bash/command payload.

The version-pinned exec handler confirms the canonical hook payload naming:
https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs
The next investigation is the isolated native command launch path, before claiming
background lifecycle coverage or adopting an automatic restart boundary.
