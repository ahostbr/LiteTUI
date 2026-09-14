# C8 same-thread model refresh remains unproven

Two bounded, isolated, two-turn Astra/medium tests are retained. Both preserve one
native thread and finish both turns. No delegation was enabled. Only synthetic MCP
tools and discovery are requested; approval policy is never and sandbox read-only.
Temporary credentials are copied into the isolated home and removed at cleanup.

1. mcp-two-turn-live.json: original fixture omitted read-only annotations. One
   native MCP completion item was observed, but no fixture call log existed.
   Acceptance is false; FileNotFoundError reflects missing call evidence, not a
   confirmed engine failure cause. This run cannot establish hook/policy cause.
2. mcp-two-turn-readonly-live.json: the fixture truthfully marks tools read-only,
   non-destructive and closed-world. The first turn executes alpha with the version
   1 string schema; the fixture records valid=true and native status is completed.
   Before turn two, alpha changes to integer and beta is added; the fixture emits
   tools/list_changed. Turn two finishes but executes neither updated alpha nor
   beta. The MCP fixture observed tools/list only at version 1. Acceptance is false.

No mcpServerStatus/list or config reload was issued between model turns. This is
deliberate: it separates notification-only model behavior from the earlier direct
catalog/status observations. These results do not refute a supported explicit
reload or configuration-revision path; those require their own behavioral proof.
The current result rules out treating the earlier catalog-status passes as proof
that ordinary model turns automatically consume changed schemas.

The read-only run records no interactive requests and no PreToolUse helper log,
despite a successful native MCP call. This is an observed coverage gap to investigate,
not yet a proven diagnosis of why the hook did not run. Do not rely on this gate as
verified MCP enforcement. Native MCP policy/hook integration remains a C1 blocker
for adopting this route. Other tool classes and child hooks are not proved here.

Both app-server processes closed and both temporary homes were removed. Recorded
usage is native numeric token accounting, including cache reads and explicit zero
cache writes; no prompt, argument, result or free-form error text is saved. Two
offline tests exercise the exact generated gate and fixture, including rejecting
string input after alpha changes to integer, permitting integer input and beta,
denying non-synthetic tool names, and excluding sentinel argument data from logs.
Ruff passes the two probe scripts and tests.

Next: investigate native MCP hook coverage, then evaluate the documented explicit
reload boundary with a synthetic config revision. No host bridge migration or
release clearance follows from these failed acceptance probes.
