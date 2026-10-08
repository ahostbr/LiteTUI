# Explicit small-task dispatch

`small_task` is an opt-in host tool for a **registered, owned leader or
orchestrator seat**. It launches one ordinary persistent LiteTUI worker visibly
through the existing `liteharness` resolver, owned-launch and fleet-policy path.
It does not classify messages, call an approval judge, inherit the leader model,
load a local model, or change the leader's backend, conversation or authority.
Claude host-tool exposure and the standard plugin registry use the same handler.

## One configuration step

In your existing LiteTUI **device settings.json**, add this one property before
starting the leader session (merge it into the existing object, do not replace
other settings):

```json
"small_task_route": {
  "backend": "claude",
  "model": "claude-haiku-5-5",
  "cognitive": "Gamma",
  "thinking_level": "low"
}
```

This is a user choice/example, not a runtime model default. Use the exact model,
bare cognitive profile and thinking level supported by your configured provider
and installed launcher; a chosen Codex route uses `"backend": "codex"` and its
exact model ID. The settings location is the existing path returned by
`litetui.settings.settings_path()`, not a new routing file. An absent/null
`small_task_route` means off. The accepted operation keeps its configuration
snapshot even if settings change later. Existing auth/provider settings are
reused, never copied into a brief or journal. There is no provider fallback.

## Dispatch contract

The leader prepares a clean, separate Git worktree and a fresh persistent worker
name, then invokes the tool with `action: "dispatch"` and a `request` object:

```json
{
  "task_id": "T-small-example",
  "purpose": "Fix the small label typo",
  "scope": "Only correct the label; no unrelated changes",
  "production_line_budget": 20,
  "allowed_files": ["src/label.py", "tests/test_label.py"],
  "acceptance_checks": ["python -m pytest tests/test_label.py -q"],
  "worktree": "<absolute-prepared-worktree-path>",
  "worker_name": "Gamma-Small-Example",
  "pane": "<visible-target-pane-id>",
  "leader_id": "<current-registered-leader-UUID>"
}
```

The budget must be an integer from 1 to 100. Paths must be literal repo-relative
files without escapes, linked ancestors or Git/agent metadata paths. Shared,
nested, main-checkout, dirty, detached or unverifiable workspaces are unavailable.
The worktree must already exist; dispatch never creates or removes it. A narrow
first slice conservatively counts **all additions plus deletions, including test
changes**, when validating a candidate. Binary changes require owner review.

These checks and the worker brief are a **scope contract, not sandbox enforcement**.
A worker still runs under the host's existing tool policy and authority. The new
tool declares full delegated capabilities rather than pretending to be read-only.
The external launcher owns profile/provider/model/auth/bridge checks, persistent
name freshness and fleet admission. It must not be replaced with a headless or
one-shot adapter. Existing names are not resumed or retasked. Local backends,
automatic worktree lifecycle and idle-seat reuse are deliberately unsupported.
The requested visible pane goes through the ordinary launcher's placement policy.

## Durable states and return receipt

The owning AgentSession stores `small-task-dispatch.sqlite` in its agent home.
Before any external launch call it atomically reserves the task, worker name and
worktree, with operation UUID, parent agent/conversation, leader return address,
branch/baseline and immutable route/scope. No mutable runtime authority is inferred
from the journal path. The journal is operational data, not configuration.

- `unavailable`: validation/configuration/storage/launcher preflight failed, or an
  invalid request/receipt was rejected. Inspect status if a prior attempt exists.
- `unknown`: launch was durably reserved, but delivery/identity is uncertain. This
  includes nonzero launcher exits, since some refusals happen after seat creation.
- `dispatched`: launcher returned a child UUID. This is not task completion.
- `candidate-ready`: leader recorded the matching worker report and Git verifies
  its commit, clean branch HEAD, baseline ancestry, files and conservative budget.
- `failed`: leader recorded the matching worker's failure report.

The worker must edit only its scope, run focused checks, self-review, commit in
its own worktree with identity/task trailers, then send a JSON receipt by inbox
to the captured leader. The leader invokes `action: "receipt"` with that object:

```json
{
  "operation_id": "<dispatch-operation-UUID>",
  "task_id": "T-small-example",
  "agent_id": "<returned-child-UUID>",
  "worktree": "<exact-worktree-from-dispatch>",
  "branch": "<exact-branch-from-dispatch>",
  "state": "candidate-ready",
  "commit": "<full-40-character-commit-SHA>",
  "changed_files": ["src/label.py", "tests/test_label.py"],
  "checks": [{"command": "python -m pytest tests/test_label.py -q", "outcome": "passed"}],
  "limits": ["No live integration verification"]
}
```

A failure receipt uses `state: "failed"`, may use `commit: null`, and still
provides check outcomes and limitations. Candidate checks are **worker-reported**,
not independently rerun by the dispatcher. The stored result includes Git numstat
and the leader recording identity. The receipt is not an authenticated direct
child callback: its operation/child/worktree binding is checked against the
journal and the leader remains responsible for verifying the inbox sender.
No result means merged, released, Done or seen by the human.

## Unknown outcomes: owner reconciliation, never automatic relaunch

Use `{"action":"status","task_id":"T-small-example"}` after interruption or
restart. The same accepted task returns its record rather than spawning again;
a different contract/name/worktree collision is refused. Status and receipt
recording remain available when routing is disabled.

For `unknown`, the owner inspects the ordinary launcher's fleet/name index,
visible panes and orphan-session diagnostics and contacts any existing named
seat. Do not delete the journal reservation, retry the launch, or choose a new
name to evade it. This slice intentionally has no reset/retry/rebind operation.
If the owner establishes that no child exists, any replacement task requires a
separate explicit owner decision and a separately prepared scope/identity.
Raw launcher/provider output is not persisted or returned because it may contain
credentials; the dispatcher reports an opaque refusal/ambiguity instead.

## Evidence boundary

Focused tests exercise real isolated Git fixtures and owned AgentSession storage,
with a fake external process boundary. They prove argv, state/identity binding,
validation, concurrency and no-retry behavior—not provider availability, visible
placement, cognitive-profile adoption or successful worker execution. Those need
an independently reviewed, leader-owned live scratch probe. No all-backend claim
is made. Human merge and human-look gates remain outside this tool.
