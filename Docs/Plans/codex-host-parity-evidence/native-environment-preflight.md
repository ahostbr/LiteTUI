# Execution-environment preflight

Task: REL-20260914-CODEX-PARITY

The installed experimental protocol exposes Thread.environments and the read-only
environment/status method. A null selection is unreported; an empty list means no
selected environment. Environment status is distinct from thread selection.

scripts/codex_environment_probe.py starts an isolated app-server with unified_exec
enabled and delegation disabled, opens a read-only/never-approval thread, then
inspects its reported environment IDs without saving the IDs. No model turn or
native command is started. Native-environment-preflight.json reports one selected
environment with status ready, successful process close and temporary-home cleanup.

This weakens the missing-environment hypothesis for the earlier command probes
but does not establish availability at execution time or prove process launch.
The version-pinned handler has pre-launch environment, argument, shell and
permission failure paths:
https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/unified_exec/exec_command.rs
Environment resolution is defined at:
https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/mod.rs

No raw internal response events were enabled, no user configuration changed and no
background/child acceptance gate was closed. Further work must identify the actual
command failure, not equate environment readiness with a successful launch.
