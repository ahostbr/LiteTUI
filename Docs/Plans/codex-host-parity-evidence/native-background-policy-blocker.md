# Native background command launch blocker

Task: REL-20260914-CODEX-PARITY

The earlier diagnostic only examined function_call_output in the legacy sessions
directory. The native result was a custom_tool_call_output. The probe now also
uses the thread's reported rollout path, constrained to its own temporary home,
and classifies both result types without retaining raw result text.

native-background-reported-rollout.json records a trusted exact-command gate that
allowed the command and completed. No commandExecution lifecycle appeared. The
actual native output contains the fixed categories failed, createprocess, policy,
rejected and blocked. This establishes a reported process-creation policy rejection
instead of evidence that a running command's lifecycle notifications were lost.
The diagnostic does not retain the raw explanation or identify the rejecting
policy component, so those details remain unknown.

The live acceptance result remains false. The app-server closed and its temporary
home was removed. A separate read-only preflight reported one ready environment;
environment readiness does not imply permission to launch the synthetic command.

Sentinel was notified with receipt b9398081-3d83-4805-970c-f4e93d78fef5. Further
command reroutes or retries must not bypass the rejection. The live background
gate needs an approved execution environment or review resolution. Automatic
inventory refresh via restart remains disabled/unaccepted. The separate native
child rerun hold remains in force. Other authorized plan work can continue.

Validation: 12 runtime tests and scoped Ruff pass. The new regression verifies
custom output classification, omission of private payload text, and refusal to
read an authoritative-looking path outside the probe home. No release, merge or
push occurred.
