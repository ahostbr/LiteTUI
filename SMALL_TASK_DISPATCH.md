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
and installed launcher. `backend` may be any name LiteTUI lists under `/backend`;
none is refused by name (see "Which routes are accepted" below). The settings
location is the existing path returned by
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
changes**, when validating a candidate. Renames are evaluated with rename
detection disabled: both the source deletion and destination addition must be
allowed and listed in the receipt, and both count toward the budget. Binary
changes require owner review.

These checks and the worker brief are a **scope contract, not sandbox enforcement**.
A worker still runs under the host's existing tool policy and authority. The new
tool declares full delegated capabilities rather than pretending to be read-only.
The external launcher owns profile/provider/model/auth/bridge checks, persistent
name freshness and fleet admission. It must not be replaced with a headless or
one-shot adapter. Existing names are not resumed or retasked. Automatic worktree
lifecycle and idle-seat reuse are deliberately unsupported.
The requested visible pane goes through the ordinary launcher's placement policy.

## Which routes are accepted: any backend, and nothing is ever loaded

Dispatch never loads a model, never starts a server and never asks one to load.
Before it reserves or launches anything it must be able to show that starting
the worker and letting it work cannot load a model. If it cannot show that, the
answer is `unavailable` with `Loading needs approval:` and a sentence saying
why. A refused route launches nothing and reserves nothing.

The check is one read-only question to the model server ("which models are
loaded right now?"): one GET request for LM Studio, Strata, NInfer and a custom
server, two to four GET requests for llama.cpp (it also reads the server's
properties). It is taken when you dispatch, from the saved settings file a new
worker reads at startup, not from the leader's live session.

| Backend in the route | Accepted when | Refused when |
|---|---|---|
| `claude`, `codex`, `cline`, `free` | always (fixed public services) | never by this check |
| `custom`, server at a public address | always; the server is not asked anything | no URL saved, or the URL is not valid |
| `custom`, server on this machine or a private network | the server reports a state for every model, this exact model is loaded, no other model is loaded and none is loading | it reports no state, or any other state below |
| `lmstudio` | this exact model is loaded, no other model is loaded and none is loading | any other state below |
| `strata` | same as LM Studio; a model row with no status is never read as loaded | any other state below |
| `ninfer` | the running engine serves this exact model | any other state below |
| `llamacpp`, a server started with one model | it serves this exact model and does not report that it is sleeping | any other state below |
| `llamacpp`, a router LiteTUI started on this machine | this exact model is loaded, no other model is loaded and none is loading | any other state below |
| `llamacpp`, any other router (LiteSuite's, a hand-started one, one on another machine) | never: it may load a model when asked | always |

"Any other state" means: the model is not loaded, the model is still loading,
a different model is loaded, the server reports a state it does not recognise
or a listing that cannot be read, the server answers with an error, or the
server cannot be reached. Every one of these is the same `unavailable` answer.

A custom address counts as public only when every address its name resolves to
is public. `localhost`, `127.1`, `[::1]`, `0.0.0.0`, a home or office network
address, a name that resolves to any of those, and a name that does not resolve
at all are treated as this machine.

### The worker checks again before every model request

Dispatch looks once; the worker runs for a long time. LM Studio, Strata and some
custom servers unload an idle model and load it again when the next request
arrives, and a llama.cpp server started with one model can be set to sleep when
idle and reload on the next request. So a worker seat that was launched as a
spawn, on LM Studio, on Strata, on a llama.cpp server started with one model, or
on a local custom server that reports a state per model, asks the same read-only
question and refuses with a sentence when its model is not loaded. It asks:

- before every model round of a turn (a turn that calls tools has several
  rounds, and each one asks again, not only the first);
- before the thinking-level probe it sends once at launch, on LM Studio;
- before it folds a large tool result through the model. On a refusal the
  result is masked instead, as it already is when that side call fails.

Where the caller sets a time limit for readiness (compaction, the launch
prompt), the question spends that limit; it does not add to it. Otherwise it
costs one request per round (two on llama.cpp): up to 5 seconds on LM Studio
and 10 on the others if the server hangs, and then the round is refused.

Other seats are unchanged: a seat a person started, a conversation born in a
spawned seat that a person later resumes by hand, and spawned leaders, thinkers
and reviewers still load on their first turn as before. A llama.cpp router and
NInfer get no such question: a router's own readiness check already refuses an
unloaded model before every request, and NInfer cannot unload.

### What is NOT proven

- **A gap of milliseconds.** Between the worker's check and its request the
  server can still unload the model, and the request would then load it. Only
  the server can close that gap. The one-line summary a worker writes on its
  answer card right after a reply is sent without asking again: it falls in
  this same gap.
- **What a real server does.** Tests use stand-ins. They do not show that a real
  LM Studio, Strata or router reports "loaded" only for a model that is in
  memory, that LM Studio calls an unloaded model `not-loaded`, that a sleeping
  llama.cpp server says `is_sleeping` in its properties (both words are from
  memory of those products' documentation, not read from a running server), or
  that a router LiteTUI started still refuses to load on a request. That needs
  one approved run against a model that is already loaded.
- **A sleeping model behind a router.** A local custom server that is a
  llama.cpp router started by hand with an idle-sleep setting can report a
  model as loaded while its weights are dropped. Only the one-model server's
  own "sleeping" flag is read. LiteTUI's own router is started without that
  setting.
- **A stale ownership record.** "A router LiteTUI started" is read from a small
  record file plus a check that the recorded process number is alive. A record
  left by a LiteTUI that was killed, a reused process number and a hand-started
  router on the same port would pass. That router's own readiness check still
  refuses an unloaded model before each request.
- **A public name that is really this machine.** A tunnel (ngrok, cloudflared),
  a port forwarded by the home router, or this machine's own public IPv6
  address reads as public, so the server is accepted and never asked.
- **A name that is public now and private later** (a VPN that changes what a
  name resolves to). The worker resolves the name again before each round, but
  it stands down if the server it then finds reports no state.
- **That the worker reads the same settings.** The check reads this process's
  saved settings. A worker started with a different data folder or different
  `LITETUI_*_HOST` variables may talk to another server; its own check before
  each request is then the only guard, and router and NInfer seats have none.
- **A llama.cpp server that stops after the check.** The new worker then starts
  its own llama.cpp router when it connects, as any LiteTUI seat does. That
  starts a process and loads no model; the worker's turns fail until one is
  loaded by a person.
- **Proxies and redirects.** A public address reached through a proxy, or one
  that redirects to this machine, is treated as public.
- **The worker's own tools.** A worker with a shell can still contact a local
  model server itself. This check is about the worker's model requests only.
- **Changing the thinking level in a worker, then reconnecting**, makes an LM
  Studio seat send its probing chat requests at connect, without asking first.
  (At launch the same probe does ask first.) A dispatched worker is told not to
  change the level; nothing enforces that.

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
Terminal receipt acceptance is atomic: an identical receipt is idempotent, while
a different concurrent or later receipt cannot overwrite the accepted result.
No result means merged, released, Done or seen by the human.

## Unknown outcomes: owner reconciliation, never automatic relaunch

Use `{"action":"status","task_id":"T-small-example"}` after interruption or
restart. The same accepted task returns its record rather than spawning again;
a different contract/name/worktree collision is refused. Status and receipt
recording remain available when routing is disabled. Recovery is **agent-wide**,
not conversation-isolated: the same owned registered leader can inspect and
record receipts after changing conversations. The original parent conversation
and return destination remain captured in the immutable dispatch record.

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
an independently reviewed, leader-owned live scratch probe. The accept/refuse
table above is proven against stand-in servers only; see "What is NOT proven".
Human merge and human-look gates remain outside this tool.
