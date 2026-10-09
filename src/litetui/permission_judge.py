"""The ONE insertion in front of the approval route, stage 1: it COUNTS. (T0408-L)

The user, 8 October 2026, on the permission judge: "Build it, and let it allow plain
reads inside a seat's own folder". Asked which way it goes, he chose "Stage 1 first:
count only". So this module authorizes NOTHING. `stands_in` is awaited from one place,
`LiteTUI._authorize_action`, after the policy has said CONFIRM and the route is known,
and it ALWAYS answers False: every approval is asked exactly as it is asked today. What
it adds is one line in the runtime log for each CONFIRM it is shown, saying whether the
deterministic check (`plain_read.assess`) would have covered that approval.

No model, no network, no approval call, no memory between calls: its False is not a
judge's vote and nothing counts it as one. Whatever goes wrong inside, an unknown value,
a missing attribute, an exception, the answer is False and the route below runs.

WHEN A ROW IS WRITTEN. The setting `permission_judge` must hold the word "count" (the
default is null, which is off: then nothing is read and nothing is written). With it on,
each CONFIRM that reaches the insertion gets exactly one row, best-effort (a failed
write changes nothing). The row is one of three kinds:

    counted       all the conditions below hold and the check says eligible
    not-eligible  all the conditions hold and the check says no, with its ONE reason code
    excluded      a condition failed; the row carries the NUMBER of the first that did.
                  The check is NOT consulted for such a call: a strict seat, a native
                  shell tool and a seat with no current spawner never reach `assess`

THE CONDITIONS, numbered as in the plan (judge/INSERTION-PLAN.md, revision 2). Number 1
is the setting. They are tried in this order and the first failure decides:

 2. The EFFECTIVE profile is interactive exactly: `decision.profile`, the profile the
    policy really judged under, never the door's optional `profile` argument. A human
    who chose strict asked to be asked (the policy owner's invariant 6).
 3. The route is "spawner" AND the seat is registered now AND has a current spawner.
    `seat_authority.confirm_route` answers "spawner" from the launch record even when
    the registry is broken, and that case ends today in "absent", a refusal; so the
    route alone proves nothing.
 4. The tool is LiteTUI's own registered `bash` or `powershell` and its shell is real.
    See THE TOOL below.
 5. The folder is proved: the seat's workspace as the app itself recorded it at launch
    (`app._hook_workspace`, set once by `hook_host.initialize` from the process's
    directory, never taken from a tool call's arguments), the seat's name, and the
    directory the core tool will run in (`Path.cwd()`, what `core_tools._run_shell`
    passes), which must lie inside a root `worktree_scope.own_roots` gives this seat.
 6. The call is not the hook Test button (`hook_test`).

THE TOOL (condition 4). The door is handed a NAME and a policy, not the callable that
will run, and the name does not tell a core tool from a native one: the Codex backend
sends every native shell call to the door under the lower-case name "bash" with the very
policy object the core tools register (codex_native_policy.py `identity`), and the
Claude backend sends its own as "Bash". What the door can see is narrower and is ALL
required:

  * the call carries NO `workspace` argument. Of the five callers of the door only
    `_execute_tool` and the Codex approval prompt (name "codex_approval") pass none;
    the Claude bridge, the Codex native hook and the hook runner always pass one. And
    `_execute_tool` runs exactly the registry's entry for the name;
  * the name is exactly "bash" or "powershell";
  * the registry holds one entry of that name, owned by core-tools, whose callable is
    `functools.partial` of `core_tools.tool_bash` / `tool_powershell` BY OBJECT
    IDENTITY, and the policy handed to the door IS that entry's policy;
  * for bash, `core_tools.bash_exe()` is not None: with no bash on the machine the tool
    hands the text to cmd.exe, a grammar the check does not hold.

This is identity of the REGISTRY'S object plus a fact about call sites, not identity of
the callable in hand. tests/test_permission_judge.py pins that fact: it counts every
attribute reference and every string constant `_authorize_action` in every module of
the package, so a sixth caller written as a call, as an alias (`door =
app._authorize_action`), as `getattr` with the literal name or as a `partial` fails a
test instead of being counted. It does NOT see a name built at run time, or a caller
outside the package; and it pins that the two native callers cannot hand the door a
workspace of None. A call that GIVES a workspace is never counted, even when it names
the seat's own folder. The row's tool class says what was
seen: core-bash, core-powershell, native-shell (a shell call that is not provably the
core tool) or other.

WHAT A ROW HOLDS. `runtime_log` accepts a closed list of keys and bounded tokens, so the
facts ride its existing keys. Never the command text, a path, a file name or a tool's
own name (a name is its author's text, not a word from a closed set):

    event       permission_judge_observation
    site        app.authorize
    component   permission_judge
    status      counted | not-eligible | excluded   (never a relay word)
    name        the tool class
    channel     the route: own | host | spawner | refuse | hand | unknown
    id          the effective profile, or unknown
    method      the shell kind                    counted, not-eligible
    operation   the check's verbs joined by +     counted
    count       how many operands the check saw   counted
    error_type  the check's reason code           not-eligible
    exit_code   the first failed condition, 2-6   excluded

FILTER ON `event` FIRST. Three of these keys mean something else on every other row of
the log: here `exit_code` is a condition number on a call where nothing exited,
`error_type` is a reason on a call where nothing failed, and `id` is a profile where an
`approval_relay` row holds an approval's id.

CEILINGS, stated and not solved here.
  * The setting is a device setting: one file, settings.json, shared by every LiteTUI
    on the machine. A seat's own write tool can change that file on the interactive
    profile, so in stage 1 a seat can switch its own counting on. That gains it log
    rows and no authorization. For stage 2 the switch must live where no seat can
    write, and that is a question for the policy owner, not solved here. The policy
    lane's word for stage 2: the switch must be a LAUNCH fact set by the owner's
    launcher or the harness resolver, like --tool-profile, never a settings key.
    Nothing of that is built.
  * Condition 5 reads the folder at CHECK time and the tool reads it again when it
    runs. That is harmless while the answer is always no. It must be closed before
    stage 2.
  * Every ceiling of `plain_read` (its docstring) applies to the word "counted".
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

from litetui import approval_relay, plain_read, runtime_log, tool_policy, worktree_scope
from litetui.plugins import core_tools

#: The setting, and the one value that turns the counting on.
SETTING = "permission_judge"
COUNT = "count"

EVENT = "permission_judge_observation"
COUNTED, NOT_ELIGIBLE, EXCLUDED = "counted", "not-eligible", "excluded"
#: The one route stage 1 looks at, and every answer `confirm_route` can give.
ROUTE = "spawner"
ROUTES = frozenset({"own", "host", "spawner", "refuse", "hand"})
NATIVE_SHELL, OTHER = "native-shell", "other"
_MAX_TOKEN = 120                     # runtime_log refuses a string over 128 characters

#: Core tool name -> the function the registry must hold for it, and the lookup that
#: says whether its shell exists. Named, not captured, so a test can stand in for either.
_CORE = {"bash": ("tool_bash", "bash_exe"), "powershell": ("tool_powershell", "powershell_exe")}


def _core_shell(app, name, policy, workspace) -> str | None:
    """"bash" or "powershell" when this call is provably LiteTUI's own registered core
    shell tool (see THE TOOL), else None. Reads the registry; changes nothing in it
    (`dispatch_for` would mark the tool as activated)."""
    if workspace is not None or type(name) is not str or name not in _CORE:
        return None
    entries = [entry for entry in app.plugins.tools if entry.name == name]
    if len(entries) != 1:
        return None
    entry = entries[0]
    run = entry.run
    if (entry.owner != core_tools.PLUGIN.id or type(run) is not functools.partial
            or run.func is not getattr(core_tools, _CORE[name][0])
            or run.args or set(run.keywords) != {"agent_id"}):
        return None
    if policy is not entry.policy or policy is not tool_policy.SHELL_POLICY:
        return None
    return name


def _tool_class(core: str | None, name, policy) -> str:
    if core is not None:
        return "core-" + core
    if (policy is tool_policy.SHELL_POLICY and isinstance(name, str)
            and name.lower() in _CORE):
        return NATIVE_SHELL
    return OTHER


def _has_current_spawner(app) -> bool:
    """The two facts `approval_relay.ask_spawner` needs before it sends: a seat that is
    registered now, and a spawner in its current presence."""
    seat = getattr(app, "seat", None)
    if seat is None or getattr(seat, "registered", False) is not True:
        return False
    spawner = approval_relay.current_spawner(app)
    return isinstance(spawner, str) and bool(spawner)


def _real(path) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _folder(app) -> tuple[Path, Path, str] | None:
    """(the seat's workspace, the directory the core tool runs in, the seat's name) when
    all three are known and the directory lies inside one of the seat's own roots."""
    workspace = getattr(app, "_hook_workspace", None)
    seat_name = getattr(getattr(app, "seat", None), "name", None)
    if not isinstance(workspace, Path) or not isinstance(seat_name, str) or not seat_name.strip():
        return None
    try:
        cwd = Path.cwd()
        here = _real(cwd)
        roots = [_real(root) for root in worktree_scope.own_roots(workspace, seat_name)]
    except (OSError, ValueError):
        return None
    if not any(here == root or here.startswith(root.rstrip("\\/") + os.sep) for root in roots):
        return None
    return workspace, cwd, seat_name


def _row(app, name, args, decision, policy, workspace, route, hook_test) -> dict | None:
    """The one row for this call, or None when no row is owed (the setting is off, or
    the decision is not a CONFIRM). Reads only."""
    if getattr(getattr(app, "settings", None), SETTING, None) != COUNT:
        return None
    if type(decision) is not tool_policy.PolicyDecision or decision.action != tool_policy.CONFIRM:
        return None
    core = _core_shell(app, name, policy, workspace)
    profile = decision.profile
    row = {"site": "app.authorize", "component": "permission_judge",
           "name": _tool_class(core, name, policy),
           "channel": route if isinstance(route, str) and route in ROUTES else "unknown",
           "id": profile if isinstance(profile, str) and profile in tool_policy.PROFILES
           else "unknown"}

    def excluded(condition: int) -> dict:
        return {**row, "status": EXCLUDED, "exit_code": condition}

    if profile != tool_policy.INTERACTIVE:
        return excluded(2)
    if route != ROUTE or not _has_current_spawner(app):
        return excluded(3)
    if core is None or getattr(core_tools, _CORE[core][1])() is None:
        return excluded(4)
    folder = _folder(app)
    if folder is None:
        return excluded(5)
    if hook_test is not False:
        return excluded(6)
    seat_workspace, cwd, seat_name = folder
    # The check is consulted HERE and nowhere else, only after every condition held.
    answer = plain_read.assess(core, args, core, seat_workspace, seat_name, cwd=cwd)
    row["method"] = core
    if not answer.eligible:
        return {**row, "status": NOT_ELIGIBLE, "error_type": answer.reason}
    verbs = "+".join(answer.verbs)
    return {**row, "status": COUNTED, "count": len(answer.operands),
            "operation": verbs if len(verbs) <= _MAX_TOKEN else f"{len(answer.verbs)}-stages"}


async def stands_in(app, name, args, decision, *, policy=None, workspace=None, route=None,
                    source=None, hook_test=False) -> bool:
    """May this call go ahead WITHOUT the approval the route below would ask for?

    Stage 1: never. It writes the one observation row (see the module text) and answers
    False, whatever it was shown and whatever failed. `source` is the turn source the
    door read; stage 1 does not use it.

    It catches Exception and NEVER BaseException: a cancellation (Esc, stop) must pass
    through untouched, here and in the door's own wrapper around this call.
    """
    try:
        row = _row(app, name, args, decision, policy, workspace, route, hook_test)
        if row is not None:
            runtime_log.record(EVENT, **row)
    except Exception:  # noqa: BLE001 - no failure here may alter today's routing
        pass
    return False
