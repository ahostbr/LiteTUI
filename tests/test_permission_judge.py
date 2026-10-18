"""T0408-L, stage 1: the count-only insertion, through the REAL approval door.

The user chose "Stage 1 first: count only". So the claim under test is one sentence:

    With the setting on, the door asks, refuses, relays and stops EXACTLY as it does
    with the setting off; the only thing added is one row of one named log event for
    each CONFIRM the insertion is shown.

Every test below sends a call through `LiteTUI._authorize_action` twice, off and on, on
the same seat, and compares everything the door did (`judge_door.same_but_for_rows`).
Then it reads the row. A row is one of three kinds: counted, not-eligible, excluded.

The policy owner's named cases are here by name: the jobs-file ownership refusal, a
stale or missing spawner, strict, both core shells, the native Bash exclusion, a cwd
outside the seat's folder, and the `workspace` argument disagreeing. The last test of
the file sends the whole case table of tests/test_plain_read.py through the door.

Every command is DATA. The door only decides; no tool runs.
"""
from __future__ import annotations

import ast
import contextlib
import functools
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

import judge_door as door
import test_plain_read as table
from litetui import app as app_mod
from litetui import approval_relay, hook_host, plain_read, runtime_log, settings, settings_scope
from litetui import permission_judge as pj
from litetui import tool_policy as tp
from litetui import worktree_scope as ws
from litetui.plugins import core_tools
from test_plain_read import tree  # noqa: F401 - the fixture: a seat's worktree and its neighbours

BASH, PS = "bash", "powershell"
READ = "cat a.txt 2>&1"                              # asked today on interactive, eligible
PS_READ = "Get-Content a.txt || Get-ChildItem"       # the same, in PowerShell
SRC = Path(pj.__file__).resolve().parent
CLASSES = {"core-bash", "core-powershell", pj.NATIVE_SHELL, pj.OTHER}
needs_powershell = pytest.mark.skipif(core_tools.powershell_exe() is None,
                                      reason="no PowerShell on this machine: the tool is not registered")


@pytest.fixture
def seat(tmp_path, monkeypatch, tree):  # noqa: F811
    """`seat(where, ...)` -> a spawned seat launched in tree[where], cwd set to it."""
    with contextlib.ExitStack() as stack:
        def make(where: str = "wt", **shape):
            monkeypatch.chdir(tree[where])
            return stack.enter_context(door.seat_app(tmp_path, tree[where], **shape))

        yield make


def row_of(on: dict) -> dict:
    """The ONE observation row of a call, checked against the log's own rules."""
    assert len(on["observed"]) == 1, on["observed"]
    row = on["observed"][0]
    runtime_log.sanitize_event({"event": pj.EVENT, **row})        # the real sink would take it
    assert row["site"] == "app.authorize" and row["component"] == "permission_judge"
    assert row["status"] in (pj.COUNTED, pj.NOT_ELIGIBLE, pj.EXCLUDED)
    assert row["name"] in CLASSES and row["channel"] in pj.ROUTES | {"unknown"}
    assert set(row) <= {"site", "component", "status", "name", "channel", "id", "method",
                        "operation", "count", "error_type", "exit_code"}
    return row


def excluded(on: dict, condition: int, tool_class: str) -> dict:
    row = row_of(on)
    assert (row["status"], row["exit_code"], row["name"]) == (pj.EXCLUDED, condition, tool_class), row
    assert on["assessed"] == 0, "an excluded call never reaches the check"
    assert not {"method", "operation", "count", "error_type"} & set(row)
    return row


# ── counted: the one class, both core shells ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", ["denied", "approved"])
async def test_the_one_class_is_counted_and_the_spawner_is_still_asked(seat, monkeypatch, relay):
    app, wire = seat(), door.Wire(monkeypatch, relay=relay)
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    assert len(off["asked"]) == len(on["asked"]) == 1, "the approval is asked, off and on"
    assert on["authorized"] is (relay == "approved"), "only the SPAWNER's word authorizes"
    assert on["stop"][0] is (relay == "denied")
    assert (off["observed"], off["assessed"]) == ([], 0), "off: nothing is read, nothing written"
    assert row_of(on) == {
        "site": "app.authorize", "component": "permission_judge", "status": pj.COUNTED,
        "name": "core-bash", "method": BASH, "operation": "cat", "count": 1,
        "channel": "spawner", "id": tp.INTERACTIVE}
    assert on["assessed"] == 1


@needs_powershell
@pytest.mark.asyncio
async def test_the_one_class_in_powershell_is_counted(seat, monkeypatch):
    app, wire = seat(), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, PS, {"command": PS_READ})
    assert door.same_but_for_rows(off, on) and len(on["asked"]) == 1
    row = row_of(on)
    assert (row["status"], row["name"], row["method"]) == (pj.COUNTED, "core-powershell", PS)
    assert (row["operation"], row["count"]) == ("get-content+get-childitem", 2)


@pytest.mark.asyncio
async def test_a_row_names_no_command_path_or_file(seat, monkeypatch, tree):  # noqa: F811
    app, wire = seat(), door.Wire(monkeypatch)
    command = f"cat {table._fwd(tree['wt'])}/notes.md 2>&1 | grep -n foo"
    _, on = await door.both(app, wire, BASH, {"command": command})
    row = row_of(on)
    assert (row["status"], row["operation"], row["count"]) == (pj.COUNTED, "cat+grep", 1)
    text = json.dumps(row)
    for secret in ("notes", "foo", "wt", str(tree["wt"].name), "cat /", "2>&1"):
        assert secret not in text, (secret, text)


@pytest.mark.asyncio
async def test_a_long_pipeline_still_fits_the_log(seat, monkeypatch):
    app, wire = seat(), door.Wire(monkeypatch)
    _, on = await door.both(app, wire, BASH, {"command": "cat a.txt 2>&1" + " | cat" * 40})
    row = row_of(on)
    assert (row["status"], row["operation"]) == (pj.COUNTED, "41-stages")


@pytest.mark.asyncio
async def test_the_row_reaches_the_real_log_file(seat, monkeypatch, tmp_path):
    app, wire = seat(), door.Wire(monkeypatch)
    recorder = runtime_log.RuntimeRecorder(tmp_path / "judge-logs" / "runtime.jsonl")
    # the Wire's stand-in for `record` is replaced by the real sink, on a file of our own
    monkeypatch.setattr(runtime_log, "record",
                        lambda event, **fields: recorder.write({"event": event, **fields}))
    try:
        await door.through(app, wire, BASH, {"command": READ})
    finally:
        recorder.close()
    lines = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    mine = [line for line in lines if line["event"] == pj.EVENT]
    assert len(mine) == 1 and mine[0]["status"] == pj.COUNTED and mine[0]["operation"] == "cat"


# ── not-eligible: asked today, and the check says no ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("command, reason", [
    ("cat .env 2>&1", plain_read.SECRET),
    ("git status 2>&1", plain_read.SHARED_STORE),
    ("cat a.txt 2>&1; rm -rf dist", plain_read.VERB),
])
async def test_an_asked_call_the_check_refuses_is_not_eligible_with_its_reason(
        seat, monkeypatch, command, reason):
    app, wire = seat(), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, BASH, {"command": command})
    assert door.same_but_for_rows(off, on) and len(on["asked"]) == 1
    row = row_of(on)
    assert (row["status"], row["error_type"], row["method"]) == (pj.NOT_ELIGIBLE, reason, BASH)
    assert "operation" not in row and "count" not in row
    assert on["assessed"] == 1


# ── excluded, condition 2: strict is never shown to the check ────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cat a.txt", READ, "ls -la"])
async def test_strict_is_excluded_and_the_check_is_never_consulted(seat, monkeypatch, command):
    app, wire = seat(profile=tp.STRICT), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, BASH, {"command": command})
    assert door.same_but_for_rows(off, on) and len(on["asked"]) == 1
    assert excluded(on, 2, "core-bash")["id"] == tp.STRICT


@pytest.mark.asyncio
async def test_the_profile_is_the_one_the_policy_judged_under_not_the_argument(seat, monkeypatch):
    """The seat's turn runs strict; a caller that hands the door `profile=interactive`
    is judged interactive (that IS the effective profile). The reverse: a turn at
    interactive with the argument strict is judged strict, and excluded."""
    app, wire = seat(profile=tp.INTERACTIVE), door.Wire(monkeypatch)
    _, on = await door.both(app, wire, BASH, {"command": READ}, profile=tp.STRICT)
    excluded(on, 2, "core-bash")


# ── excluded, condition 3: a stale or missing spawner ────────────────────────


@pytest.mark.asyncio
async def test_a_historical_spawner_and_an_unregistered_seat_stay_absent_and_are_not_counted(
        seat, monkeypatch, tmp_path):
    """`confirm_route` still says "spawner" from the launch record. Today that ends in
    `absent`, a refusal that stops the turn. It must end the same way, with no count."""
    from approval_store_fixture_t0340 import bind_origin
    app = seat(registered=False)
    bind_origin(app, tmp_path / "origin", actual_edit=True)   # the relay's audit needs a store
    wire = door.Wire(monkeypatch, relay=None)        # the REAL ask_spawner
    sent = []
    app.seat.send = lambda *a, **k: sent.append(a) or True
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    for run in (off, on):
        assert not run["authorized"] and run["stop"][0] and "not reachable" in run["stop"][1]
        assert [fields["status"] for event, fields in run["log"] if event == "approval_relay"] == ["absent"]
    assert sent == [], "nothing was sent to anyone"
    assert excluded(on, 3, "core-bash")["channel"] == "spawner"
    assert [row["status"] for row in wire.observed()] == [pj.EXCLUDED], "zero counted"


@pytest.mark.asyncio
async def test_a_registered_seat_whose_presence_names_no_spawner_is_not_counted(seat, monkeypatch):
    app, wire = seat(), door.Wire(monkeypatch)
    door.present(app, None)                           # registered, but no spawned_by NOW
    assert app._spawner_id and approval_relay.current_spawner(app) is None
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    excluded(on, 3, "core-bash")


@pytest.mark.asyncio
async def test_a_seat_with_no_spawner_at_all_is_refused_as_today_and_not_counted(seat, monkeypatch):
    app, wire = seat(spawner=None), door.Wire(monkeypatch)
    app._agent_launched = True                        # confirm_route: "refuse"
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on) and on["asked"] == [] and on["stop"][0]
    assert excluded(on, 3, "core-bash")["channel"] == "refuse"


@pytest.mark.asyncio
async def test_a_hosted_seat_asks_its_host_even_with_a_live_spawner_and_is_not_counted(
        seat, monkeypatch):
    """Route "host": an rpc seat whose host relays. It HAS a current spawner, so only
    the route keeps it out of the count."""
    app, wire = seat(), door.Wire(monkeypatch)
    app._rpc, app._approval_host = True, True
    assert approval_relay.current_spawner(app) == door.SPAWNER
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    assert on["modals"] == ["rpc"] and on["asked"] == [], "the host was asked, not the spawner"
    assert excluded(on, 3, "core-bash")["channel"] == "host"


@pytest.mark.asyncio
async def test_the_owners_own_seat_gets_its_modal_as_today_and_is_not_counted(seat, monkeypatch):
    app, wire = seat(spawner=None), door.Wire(monkeypatch)
    app._spawned_seat, app._owner_seat, app._pty_term = False, True, None   # confirm_route: "own"
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    assert on["modals"] == ["modal"] and on["asked"] == [], "his own modal, nobody relayed"
    assert excluded(on, 3, "core-bash")["channel"] == "own"


# ── excluded, condition 4: only LiteTUI's own registered core shell ──────────


@pytest.mark.asyncio
async def test_the_claude_backends_native_bash_is_excluded(seat, monkeypatch, tree):  # noqa: F811
    """claude_tools._decide: name "Bash", the shared shell policy, the segment's workspace."""
    app, wire = seat(), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, "Bash", {"command": READ}, policy=tp.SHELL_POLICY,
                              workspace=tree["wt"], stop_on_denial=False)
    assert door.same_but_for_rows(off, on) and len(on["asked"]) == 1
    excluded(on, 4, pj.NATIVE_SHELL)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["wt", "main"])
async def test_a_codex_native_shell_under_the_name_bash_is_excluded_whatever_its_workspace(
        seat, monkeypatch, tree, where):  # noqa: F811
    """codex_native_policy sends every native shell call as lower-case "bash" with the
    policy object the core tool registers. `where` = "wt": the workspace AGREES with the
    seat's and is still not counted. "main": the policy owner's DISAGREEING case."""
    app, wire = seat(), door.Wire(monkeypatch)
    registered = app.plugins.policy_for("shell_command")          # None: not a host tool
    off, on = await door.both(app, wire, BASH, {"command": READ, "cmd": READ},
                              policy=registered or tp.SHELL_POLICY, workspace=tree[where])
    assert door.same_but_for_rows(off, on) and len(on["asked"]) == 1
    excluded(on, 4, pj.NATIVE_SHELL)


@pytest.mark.asyncio
async def test_with_no_bash_on_the_machine_the_bash_tool_is_excluded(seat, monkeypatch):
    """Then `tool_bash` hands the text to cmd.exe, whose grammar the check does not hold."""
    app, wire = seat(), door.Wire(monkeypatch)
    monkeypatch.setattr(core_tools, "bash_exe", lambda: None)
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    excluded(on, 4, "core-bash")


@pytest.mark.asyncio
@pytest.mark.parametrize("swap", ["function", "owner", "policy", "bound", "twice"])
async def test_a_registry_entry_that_is_not_exactly_the_core_tool_is_excluded(
        seat, monkeypatch, swap):
    app, wire = seat(), door.Wire(monkeypatch)
    entry = next(e for e in app.plugins.tools if e.name == BASH)
    assert entry.run.func is core_tools.tool_bash and entry.owner == core_tools.PLUGIN.id
    policy = tp.SHELL_POLICY
    stand_in = {
        "function": lambda: dict(run=functools.partial(lambda args, agent_id=None: "", agent_id=None)),
        "owner": lambda: dict(owner="someone-else"),
        "policy": lambda: dict(policy=tp.ToolPolicy(tp.SHELL_POLICY.capabilities, "a copy")),
        "bound": lambda: dict(run=functools.partial(core_tools.tool_bash, {"command": "x"}, agent_id=None)),
        "twice": lambda: {},
    }[swap]()
    index = app.plugins.tools.index(entry)
    changed = type(entry)(**{**{f: getattr(entry, f) for f in ("owner", "name", "spec", "run", "gate", "policy")},
                             **stand_in})
    app.plugins.tools[index] = changed
    if swap == "twice":
        app.plugins.tools.append(entry)
    if swap == "policy":
        policy = changed.policy
    off, on = await door.both(app, wire, BASH, {"command": READ}, policy=policy)
    assert door.same_but_for_rows(off, on)
    if on["observed"]:                                # a copied policy may not ask at all
        row = row_of(on)
        assert (row["status"], row["exit_code"]) == (pj.EXCLUDED, 4) and on["assessed"] == 0


@pytest.mark.asyncio
async def test_the_tool_class_is_read_from_the_policy_object_not_from_the_name(seat, monkeypatch):
    """A tool that merely CALLS itself bash, with a policy that is not the shell policy,
    is "other"; with the shell policy and a workspace it is a native shell."""
    app, wire = seat(), door.Wire(monkeypatch)
    _, on = await door.both(app, wire, BASH, {"command": READ}, policy=tp.MCP_UNKNOWN_POLICY)
    excluded(on, 4, pj.OTHER)
    _, on = await door.both(app, wire, "PowerShell", {"command": PS_READ}, policy=tp.SHELL_POLICY,
                            workspace=Path.cwd())
    excluded(on, 4, pj.NATIVE_SHELL)


@pytest.mark.asyncio
async def test_a_tool_that_is_no_shell_is_excluded_as_other(seat, monkeypatch):
    """An MCP tool whose effects nobody declared: asked about today, and no core shell
    even when it carries a `command`."""
    app, wire = seat(), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, "mcp__litesuite-tools__shell", {"command": READ},
                              policy=tp.MCP_UNKNOWN_POLICY)
    assert door.same_but_for_rows(off, on) and len(on["asked"]) == 1, "an undeclared tool is asked"
    excluded(on, 4, pj.OTHER)


# ── excluded, condition 5: the folder ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("cwd", ["main", "plain", "other"])
async def test_a_cwd_outside_the_seats_own_folder_is_excluded(seat, monkeypatch, tree, cwd):  # noqa: F811
    app, wire = seat(), door.Wire(monkeypatch)
    monkeypatch.chdir(tree[cwd])                      # the core tool would run HERE
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    if on["observed"]:
        excluded(on, 5, "core-bash")
    else:
        assert on["asked"] == [], "no row only where the policy did not ask at all"


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["none", "plain", "missing"])
async def test_a_seat_with_no_proved_workspace_is_excluded(seat, monkeypatch, tree, launch):  # noqa: F811
    app, wire = seat(), door.Wire(monkeypatch)
    app._hook_workspace = None if tree[launch] is None else Path(tree[launch])
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    excluded(on, 5, "core-bash")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", [None, "", "  ", 7])
async def test_a_seat_with_no_name_is_excluded(seat, monkeypatch, name):
    app, wire = seat(), door.Wire(monkeypatch)
    app.seat.name = name
    off, on = await door.both(app, wire, BASH, {"command": READ})
    assert door.same_but_for_rows(off, on)
    if on["observed"]:
        excluded(on, 5, "core-bash")


@pytest.mark.asyncio
async def test_the_workspace_is_the_apps_own_record_not_a_fallback(seat, monkeypatch):
    """`codex_workspace.workspace` falls back to the current directory when the app
    recorded none. The count must not: a missing record is not a proof."""
    app, wire = seat(), door.Wire(monkeypatch)
    del app._hook_workspace
    _, on = await door.both(app, wire, BASH, {"command": READ})
    excluded(on, 5, "core-bash")
    source = (SRC / "hook_host.py").read_text(encoding="utf-8")
    assert source.count("_hook_workspace = ") == 1 and "app._hook_workspace = Path.cwd().resolve()" in source
    assert hook_host.initialize.__code__.co_firstlineno < 20, "set once, at launch"


# ── excluded, condition 6: the hook Test button ──────────────────────────────


@pytest.mark.asyncio
async def test_the_hook_test_button_is_excluded(seat, monkeypatch):
    app, wire = seat(), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, BASH, {"command": READ}, hook_test=True,
                              stop_on_denial=False)
    assert door.same_but_for_rows(off, on) and not on["stop"][0], "a hook test stops no turn"
    excluded(on, 6, "core-bash")


@pytest.mark.asyncio
async def test_a_real_hook_call_is_excluded_as_other(seat, monkeypatch, tree):  # noqa: F811
    """hook_host.invoke: an approval name that can hold a path, a cwd and an env argument."""
    app, wire = seat(), door.Wire(monkeypatch)
    args = {"command": json.dumps(["cat", "a.txt"]), "cwd": str(tree["wt"]), "env": {}}
    off, on = await door.both(app, wire, f"hook:{tree['wt']}/h.py", args, policy=tp.SHELL_POLICY,
                              workspace=tree["wt"], hook_test=True, stop_on_denial=False)
    assert door.same_but_for_rows(off, on)
    if on["observed"]:
        row = excluded(on, 4, pj.OTHER)
        assert str(tree["wt"].name) not in json.dumps(row)


# ── no row at all: what never reaches the insertion ──────────────────────────


@pytest.mark.asyncio
async def test_the_jobs_file_ownership_refusal_comes_first_and_nothing_is_counted(
        seat, monkeypatch):
    app, wire = seat("card"), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, BASH, {"command": "echo [] > jobs.json"})
    assert door.same_but_for_rows(off, on)
    assert not on["authorized"] and "jobs.json" in on["refusal"] and "T1085" in on["refusal"]
    assert (on["asked"], on["modals"], on["observed"], on["assessed"]) == ([], [], [], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("command, rule, authorized", [
    ("rm -rf @MAIN@", "", False),                     # the deny floor
    (READ, "deny", False),                            # a standing human deny on an eligible call
    (READ, "allow", True),                            # a standing human allow
    ("cat a.txt", "", True),                          # an ordinary read is never asked
])
async def test_a_deny_or_an_allow_never_reaches_the_insertion(
        seat, monkeypatch, tree, command, rule, authorized):  # noqa: F811
    app, wire = seat(), door.Wire(monkeypatch)
    args = {"command": table._fill(command, tree)}
    if rule:
        probe = tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, args, app_mod.paths.ROOT,
                            tool_name=BASH, shell=BASH, seat_name=door.SEAT)
        key = [tp.rule_key(BASH, probe.capabilities)]
        app.settings.tool_deny, app.settings.tool_always_allow = (
            (key, []) if rule == "deny" else ([], key))
    off, on = await door.both(app, wire, BASH, args)
    assert door.same_but_for_rows(off, on)
    assert on["authorized"] is authorized
    assert (on["asked"], on["observed"], on["assessed"]) == ([], [], 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [READ, "rm -rf dist", "cat a.txt"])
async def test_autonomous_never_asks_so_nothing_is_counted(seat, monkeypatch, command):
    app, wire = seat(profile=tp.AUTONOMOUS), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, BASH, {"command": command})
    assert door.same_but_for_rows(off, on) and on["authorized"]
    assert (on["asked"], on["modals"], on["observed"], on["assessed"]) == ([], [], [], 0)


@pytest.mark.asyncio
async def test_during_shutdown_the_confirm_is_refused_before_the_insertion(seat, monkeypatch):
    """`allow_prompt=False`: the door refuses a CONFIRM in words, above the insertion."""
    app, wire = seat(), door.Wire(monkeypatch)
    off, on = await door.both(app, wire, BASH, {"command": READ}, allow_prompt=False)
    assert door.same_but_for_rows(off, on)
    assert not on["authorized"] and "approval unavailable during shutdown" in on["refusal"]
    assert (on["asked"], on["modals"], on["observed"], on["assessed"]) == ([], [], [], 0)
    assert not on["stop"][0]


@pytest.mark.asyncio
async def test_only_a_confirm_decision_owes_a_row(seat, monkeypatch):
    """The insertion sits in the CONFIRM branch, so the door never shows it anything
    else. Called directly with an ALLOW, a DENY or a thing that is no decision, it
    writes nothing and still answers False."""
    app, wire = seat(), door.Wire(monkeypatch)
    shown = dict(policy=tp.SHELL_POLICY, workspace=None, route="spawner", source="typed",
                 hook_test=False)
    confirm = tp.PolicyDecision(tp.CONFIRM, tp.INTERACTIVE, frozenset(), "asked")
    assert await pj.stands_in(app, BASH, {"command": READ}, confirm, **shown) is False
    assert [row["status"] for row in wire.observed()] == [pj.COUNTED], "the control: a CONFIRM"
    for decision in (tp.PolicyDecision(tp.ALLOW, tp.INTERACTIVE, frozenset(), "allowed"),
                     tp.PolicyDecision(tp.DENY, tp.INTERACTIVE, frozenset(), "denied"),
                     None, "confirm", {"action": tp.CONFIRM, "profile": tp.INTERACTIVE}):
        assert await pj.stands_in(app, BASH, {"command": READ}, decision, **shown) is False
    assert len(wire.observed()) == 1 and wire.assessed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "", "Count", "count ", "on", "judge", True, 1, ["count"]])
async def test_only_the_word_count_turns_it_on(seat, monkeypatch, value):
    app, wire = seat(judge=value), door.Wire(monkeypatch)
    run = await door.through(app, wire, BASH, {"command": READ})
    assert len(run["asked"]) == 1 and (run["observed"], run["assessed"]) == ([], 0)


# ── nothing inside it can change the route ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["row", "check", "log-raises", "log-false", "spawner",
                                   "registry", "stands_in", "import"])
async def test_no_failure_inside_changes_what_the_door_does(seat, monkeypatch, fault):
    app, wire = seat(), door.Wire(monkeypatch)
    baseline = await door.through(app, wire, BASH, {"command": READ})
    assert row_of(baseline)["status"] == pj.COUNTED

    def boom(*_a, **_k):
        raise RuntimeError("inside the insertion")

    if fault == "row":
        monkeypatch.setattr(pj, "_row", boom)
    elif fault == "check":
        monkeypatch.setattr(plain_read, "assess", boom)
    elif fault == "log-raises":
        monkeypatch.setattr(runtime_log, "record", boom)
    elif fault == "log-false":
        monkeypatch.setattr(runtime_log, "record", lambda *_a, **_k: False)
    elif fault == "spawner":
        monkeypatch.setattr(approval_relay, "current_spawner", boom)
    elif fault == "registry":
        monkeypatch.setattr(app, "plugins", None)
    elif fault == "stands_in":
        monkeypatch.setattr(pj, "stands_in", boom)
    elif fault == "import":
        import litetui
        monkeypatch.delattr(litetui, "permission_judge")
        monkeypatch.setitem(sys.modules, "litetui.permission_judge", None)
    broken = await door.through(app, wire, BASH, {"command": READ}, policy=tp.SHELL_POLICY)
    assert door.same_but_for_rows(baseline, broken), fault
    assert broken["observed"] == [], "a failed count writes nothing"
    assert len(broken["asked"]) == 1 and not broken["authorized"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["row", "log"])
async def test_the_judge_itself_swallows_its_failures_without_the_doors_help(
        seat, monkeypatch, fault):
    """Two layers say "nothing happened": `stands_in`'s own try, and the door's wrapper
    around the call. The test above goes through both, so it cannot tell whether the
    inner one exists (measured: a mutation batch, judge/stage1-mutations-01.log). This
    one calls `stands_in` with no door around it."""
    app, wire = seat(), door.Wire(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("inside the judge")

    monkeypatch.setattr(pj, "_row", boom) if fault == "row" else monkeypatch.setattr(
        runtime_log, "record", boom)
    confirm = tp.PolicyDecision(tp.CONFIRM, tp.INTERACTIVE, frozenset(), "asked")
    answer = await pj.stands_in(app, BASH, {"command": READ}, confirm, policy=tp.SHELL_POLICY,
                                workspace=None, route="spawner", source="typed", hook_test=False)
    assert answer is False and wire.observed() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["row", "stands_in"])
async def test_a_cancellation_inside_is_never_swallowed(seat, monkeypatch, where):
    """Esc and stop arrive as CancelledError, a BaseException. Both wrappers catch
    Exception only, so it leaves the door as it would have without the insertion."""
    import asyncio
    app, wire = seat(), door.Wire(monkeypatch)

    def cancelled(*_a, **_k):
        raise asyncio.CancelledError("stop")

    monkeypatch.setattr(pj, "_row" if where == "row" else "stands_in", cancelled)
    if where == "row":
        with pytest.raises(asyncio.CancelledError):
            await pj.stands_in(app, BASH, {"command": READ}, None)
    with pytest.raises(asyncio.CancelledError):
        await app._authorize_action(BASH, {"command": READ}, app.plugins.policy_for(BASH),
                                    profile=tp.INTERACTIVE)
    assert wire.asked == [] and wire.observed() == []


def test_both_wrappers_catch_exception_and_never_base_exception():
    judge = ast.parse(Path(pj.__file__).read_text(encoding="utf-8"))
    handlers = [node for node in ast.walk(judge) if isinstance(node, ast.ExceptHandler)]
    assert [ast.unparse(h.type) for h in handlers] == ["(OSError, ValueError)", "Exception"]
    door_fn = next(node for node in ast.walk(ast.parse((SRC / "app.py").read_text(encoding="utf-8")))
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "_authorize_action")
    wrapper = next(node for node in ast.walk(door_fn) if isinstance(node, ast.Try)
                   and "permission_judge" in ast.unparse(node))
    assert [ast.unparse(h.type) for h in wrapper.handlers] == ["Exception"]
    assert not wrapper.finalbody and not wrapper.orelse


@pytest.mark.asyncio
async def test_the_reserved_exit_is_live_code_which_is_why_the_answer_is_pinned_below(
        seat, monkeypatch):
    """The insertion reads `if stood_in: return None`. If `stands_in` ever answered True
    the door would authorize with nobody asked. Stage 1's guarantee is therefore a
    property of the SOURCE of `stands_in`, which the next test pins."""
    app, wire = seat(), door.Wire(monkeypatch)

    async def yes(*_a, **_k):
        return True

    monkeypatch.setattr(pj, "stands_in", yes)
    run = await door.through(app, wire, BASH, {"command": READ})
    assert run["authorized"] and run["asked"] == []


# ── the source: one call site, one caller of the check, one answer ───────────


def _calls(path: Path, attribute: str) -> list[ast.Call]:
    return [node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == attribute]


def _modules() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_stands_in_can_only_answer_false():
    """The BINDING of the name is pinned, not the first definition found (review of
    192bb56, P1). A reading of "the first `async def stands_in`" passed with a decorator
    that answered True, with `stands_in = other` at the end of the module, and with a
    second definition after the first; the last of those no other test noticed."""
    tree_ = ast.parse(Path(pj.__file__).read_text(encoding="utf-8"))
    definitions = [node for node in ast.walk(tree_)
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and node.name == "stands_in"]
    assert len(definitions) == 1, "one definition of the name in the whole module"
    function = definitions[0]
    assert isinstance(function, ast.AsyncFunctionDef) and function in tree_.body
    assert function.decorator_list == [], "no decorator may stand between the name and the body"
    # nothing else binds the name: no assignment, no import, no loop or with target, no
    # `global`, and no string "stands_in" a setattr or a globals() write could use
    stores = [node for node in ast.walk(tree_) if isinstance(node, ast.Name)
              and node.id == "stands_in" and not isinstance(node.ctx, ast.Load)]
    aliases = [alias for node in ast.walk(tree_) if isinstance(node, (ast.Import, ast.ImportFrom))
               for alias in node.names if "stands_in" in (alias.name, alias.asname)]
    declared = [node for node in ast.walk(tree_) if isinstance(node, (ast.Global, ast.Nonlocal))
                and "stands_in" in node.names]
    spelled = [node for node in ast.walk(tree_)
               if isinstance(node, ast.Constant) and node.value == "stands_in"]
    assert (stores, aliases, declared, spelled) == ([], [], [], [])
    # and the object the door will call IS that definition
    assert pj.stands_in.__code__.co_firstlineno == function.lineno
    assert Path(pj.stands_in.__code__.co_filename).resolve() == Path(pj.__file__).resolve()
    assert pj.stands_in.__name__ == "stands_in" and not hasattr(pj.stands_in, "__wrapped__")
    assert pj.stands_in.__module__ == pj.__name__
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    assert len(returns) == 1
    assert isinstance(returns[0].value, ast.Constant) and returns[0].value.value is False
    assert function.body[-1] is returns[0], "the last statement, outside the try"
    assert not [node for node in ast.walk(function) if isinstance(node, (ast.Await, ast.Yield))]


def test_stands_in_is_called_from_one_place():
    sites = {path.name: len(_calls(path, "stands_in")) for path in _modules()}
    assert {name: n for name, n in sites.items() if n} == {"app.py": 1}
    users = [path.name for path in _modules() if path.name != "permission_judge.py"
             and "permission_judge" in path.read_text(encoding="utf-8")]
    assert sorted(users) == ["app.py", "settings.py", "settings_scope.py", "settings_screen.py",
                             "settings_ui_model.py"], users
    importers = [path.name for path in _modules()
                 if any(isinstance(node, ast.ImportFrom) and node.module == "litetui"
                        and any(alias.name == "permission_judge" for alias in node.names)
                        or isinstance(node, ast.Import)
                        and any("permission_judge" in alias.name for alias in node.names)
                        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))))]
    assert importers == ["app.py"]
    # and that one call is the reserved place: inside the door, after the route is known
    app_tree = ast.parse((SRC / "app.py").read_text(encoding="utf-8"))
    doors = [node for node in ast.walk(app_tree)
             if isinstance(node, ast.AsyncFunctionDef) and node.name == "_authorize_action"]
    assert len(doors) == 1
    inside = [node for node in ast.walk(doors[0]) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute) and node.func.attr == "stands_in"]
    assert len(inside) == 1
    call = inside[0]
    assert sorted(keyword.arg for keyword in call.keywords) == [
        "hook_test", "policy", "route", "source", "workspace"]
    lines = (SRC / "app.py").read_text(encoding="utf-8").splitlines()
    route_line = next(i for i, line in enumerate(lines)
                      if line.strip() == "route = seat_authority.confirm_route(self)"
                      and i + 1 > doors[0].lineno)
    branch_line = next(i for i, line in enumerate(lines)
                       if line.strip() == 'if route in ("spawner", "refuse"):' and i > route_line)
    assert route_line + 1 < call.lineno < branch_line + 1, "between the route and the branch on it"
    assert branch_line - route_line == 12, "eleven lines were inserted and nothing else"


def test_the_check_is_called_from_the_judge_only():
    users = [path.name for path in _modules() if path.name != "plain_read.py"
             and "plain_read" in path.read_text(encoding="utf-8")]
    assert users == ["permission_judge.py"]
    assert len(_calls(Path(pj.__file__), "assess")) == 1


def test_the_door_has_five_callers_and_only_the_tool_executor_runs_the_registrys_entry():
    """Condition 4 rests on this: a call that carries NO workspace and a core tool's name
    came through `_execute_tool`, which runs exactly the registry's entry. A new caller
    of the door changes one of the numbers here."""
    found = {}
    for path in _modules():
        for call in _calls(path, "_authorize_action"):
            first = call.args[0]
            found.setdefault(path.name, []).append((
                first.value if isinstance(first, ast.Constant) else "",
                "workspace" in {keyword.arg for keyword in call.keywords}))
    assert found == {
        "claude_tools.py": [("", True)],              # native tools, the segment's workspace
        "codex_native_policy.py": [("", True)],       # native tools as "bash", the launch workspace
        "hook_host.py": [("", True)],                 # hook processes
        "codex_app_server.py": [("codex_approval", False)],   # a fixed name that is no shell tool
    }
    # EVERY way of naming the door, not only a call written `x._authorize_action(...)`
    # (review of 192bb56, P2). An alias (`door = app._authorize_action`), a
    # `getattr(app, "_authorize_action")` and a `partial(type(app)._authorize_action, app)`
    # are each an attribute reference or a string constant, and the finder above saw none
    # of the three. NOT seen even now: a name built at run time, a caller outside `src/`.
    references, constants = {}, {}
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr == "_authorize_action":
                references[path.name] = references.get(path.name, 0) + 1
            elif isinstance(node, ast.Constant) and node.value == "_authorize_action":
                constants[path.name] = constants.get(path.name, 0) + 1
    assert references == {"claude_tools.py": 1, "codex_native_policy.py": 1, "hook_host.py": 1,
                          "codex_app_server.py": 1, "app.py": 1}, references
    assert constants == {"app.py": 1}, constants
    # in the four modules the one reference IS the call found above (no alias among them)
    assert {name: len(calls) for name, calls in found.items()} == {
        name: count for name, count in references.items() if name != "app.py"}
    # the fifth: _execute_tool reaches the door through a local name, with a profile only
    source = (SRC / "app.py").read_text(encoding="utf-8")
    app_tree = ast.parse(source)
    executor = next(node for node in ast.walk(app_tree)
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_tool")
    named = [node.lineno for node in ast.walk(app_tree)
             if isinstance(node, ast.Constant) and node.value == "_authorize_action"]
    assert len(named) == 1 and executor.lineno < named[0] <= executor.end_lineno
    via = [node for node in ast.walk(executor) if isinstance(node, ast.Call)
           and isinstance(node.func, ast.Name) and node.func.id == "authorize"]
    assert len(via) == 1 and [keyword.arg for keyword in via[0].keywords] == ["profile"]
    assert [ast.unparse(arg) for arg in via[0].args] == ["name", "args", "policy"]
    text = ast.unparse(executor)
    assert "fn = self._dispatch_for(name)" in text and "asyncio.to_thread(fn, args)" in text
    assert "policy = self.plugins.policy_for(name)" in text
    # app.py's one attribute reference and its one string constant are that same statement
    inside = [node for node in ast.walk(executor)
              if (isinstance(node, ast.Attribute) and node.attr == "_authorize_action")
              or (isinstance(node, ast.Constant) and node.value == "_authorize_action")]
    assert len(inside) == 2 and inside[0].lineno == inside[1].lineno
    assert ("authorize = getattr(self, '_authorize_action', "
            "partial(LiteTUI._authorize_action, self))") in text


def test_the_two_native_callers_can_never_hand_the_door_a_workspace_of_none(tmp_path):
    """The ONE fact condition 4 rests on (review of 192bb56, section 4): the same Codex
    call WITHOUT its workspace argument would be counted as the core tool. So the
    expression each native caller passes is pinned, and so is the reason it is never None."""
    from types import SimpleNamespace

    from litetui import claude_tools, codex_native_policy, codex_workspace, lifecycle_hooks

    def passed(module: str) -> str:
        calls = _calls(SRC / module, "_authorize_action")
        assert len(calls) == 1
        return next(ast.unparse(k.value) for k in calls[0].keywords if k.arg == "workspace")

    # Codex: the launch workspace, or the process's directory when the app recorded none
    assert passed("codex_native_policy.py") == "workspace(self.app)"
    assert codex_native_policy.workspace is codex_workspace.workspace
    for app in (None, SimpleNamespace(), SimpleNamespace(_hook_workspace=None),
                SimpleNamespace(_hook_workspace=tmp_path), SimpleNamespace(_hook_workspace=str(tmp_path))):
        assert isinstance(codex_workspace.workspace(app), Path)
    # Claude: the segment's workspace, or the install folder
    assert passed("claude_tools.py") == "self.workspace"
    source = ast.parse((SRC / "claude_tools.py").read_text(encoding="utf-8"))
    assigned = [ast.unparse(node.value) for node in ast.walk(source) if isinstance(node, ast.Assign)
                and [ast.unparse(target) for target in node.targets] == ["self.workspace"]]
    assert assigned == ["Path(workspace) if workspace else paths.ROOT"]
    host = SimpleNamespace(convo_id="c")
    for given in (None, "", tmp_path, str(tmp_path)):
        assert isinstance(claude_tools.ClaudeTools(host, None, "s", workspace=given).workspace, Path)
    # the hook runner: its tool name can never be a core tool's, whatever its workspace
    assert passed("hook_host.py") == "workspace"
    name = lifecycle_hooks.Hook.approval_name
    assert "return f'hook:{self.id}:{digest}'" in ast.unparse(ast.parse(
        (SRC / "lifecycle_hooks.py").read_text(encoding="utf-8")))
    assert callable(name)


def test_the_core_tools_are_registered_as_the_judge_expects():
    """What `_core_shell` compares against, read from the registration itself."""
    source = (SRC / "plugins" / "core_tools.py").read_text(encoding="utf-8")
    assert "ctx.tool(BASH_SPEC, partial(tool_bash, agent_id=agent_id), policy=SHELL_POLICY)" in source
    assert ("ctx.tool(powershell_spec(), partial(tool_powershell, agent_id=agent_id), "
            "policy=SHELL_POLICY)") in source
    assert core_tools.SHELL_POLICY is tp.SHELL_POLICY and core_tools.PLUGIN.id == "core-tools"
    # the tool starts its shell in the process's own directory, and hands bash the text whole
    assert "cwd=str(Path.cwd())," in source
    assert '_run_shell([exe, "-c", command], shell=False' in source


def test_the_module_reads_and_logs_and_does_nothing_else():
    source = Path(pj.__file__).read_text(encoding="utf-8")
    imported, called = set(), set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    assert imported == {
        "__future__.annotations", "functools", "os", "pathlib.Path", "litetui.approval_relay",
        "litetui.plain_read", "litetui.runtime_log", "litetui.tool_policy",
        "litetui.worktree_scope", "litetui.plugins.core_tools"}
    assert not called & {"open", "read_text", "write_text", "write_bytes", "system", "popen",
                         "Popen", "run", "urlopen", "connect", "request", "post", "get_event_loop",
                         "ask_spawner", "send", "show_dialog", "exec", "eval", "setattr",
                         "dispatch_for", "sleep", "create_task"}
    assert "record" in called and source.count("runtime_log.record(") == 1
    lowered = source.lower()
    for word in ("lms", "1234", "7470", "http", "localhost", "model_id", "backend="):
        assert word not in lowered, word


def test_the_setting_is_off_by_default_and_belongs_to_the_device():
    assert settings.Settings().permission_judge is None
    assert pj.SETTING == "permission_judge" and pj.COUNT == "count"
    spec = settings_scope.SETTING_SPECS["permission_judge"]
    assert spec.scope == settings_scope.SettingScope.DEVICE
    assert spec.scope == settings_scope.SETTING_SPECS["small_task_route"].scope
    assert settings._coerce("permission_judge", None, "count") is None
    assert "permission_judge" not in settings.ENV_OVERRIDES, "no environment switch"


# ── the whole case table of the check, through the door, off and on ──────────


def _platform_skips(case, tree) -> bool:  # noqa: F811
    """The rows tests/test_plain_read.py::test_case does not judge on this platform."""
    if case.needs == "windows" and not tree["have"]["windows"]:
        return True
    if case.needs and not tree["have"][case.needs]:
        return True
    return os.name != "nt" and isinstance(case.command, str) and (
        (case.shell == PS and "\\" in case.command) or bool(table._DRIVE_PATH.search(case.command)))


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [tp.INTERACTIVE, tp.STRICT, tp.AUTONOMOUS])
async def test_every_row_of_the_case_table_through_the_door_off_and_on(
        seat, monkeypatch, tree, profile, capsys):  # noqa: F811
    app, wire = seat(profile=profile), door.Wire(monkeypatch)
    sent = asked = 0
    kinds: Counter = Counter()
    skipped: Counter = Counter()
    for case in table.CASES:
        tool = case.tool or case.shell
        if not isinstance(tool, str) or app.plugins.policy_for(tool) is None:
            skipped["no such host tool: the executor refuses it before the door"] += 1
            continue
        launch = tree[case.where]
        runs_in = tree[case.cwd] if case.cwd else launch
        if runs_in is None or not Path(runs_in).is_dir():
            runs_in = tree["plain"]                   # nobody's worktree
        monkeypatch.chdir(runs_in)
        app._hook_workspace = None if launch is None else Path(launch)
        app.seat.name = case.seat
        args = table._fill(case.args if case.args is not None
                           else {"command": case.command, **case.extra}, tree)
        off, on = await door.both(app, wire, tool, args)
        sent += 1
        assert door.same_but_for_rows(off, on), (case.id, off, on)
        assert (off["observed"], off["assessed"]) == ([], 0), case.id
        if not off["asked"]:
            assert (on["observed"], on["assessed"]) == ([], 0), (case.id, "not asked, so no row")
            continue
        asked += 1
        row = row_of(on)
        if profile != tp.INTERACTIVE:
            excluded(on, 2, row["name"])
            kinds[(row["status"], 2)] += 1
            continue
        named = isinstance(case.seat, str) and bool(case.seat.strip())
        here = os.path.normcase(os.path.realpath(str(runs_in)))
        roots = ([os.path.normcase(os.path.realpath(str(r))) for r in ws.own_roots(launch, case.seat)]
                 if named and launch is not None else [])
        inside = any(here == root or here.startswith(root + os.sep) for root in roots)
        if tool not in (BASH, PS):
            excluded(on, 4, pj.OTHER)
        elif not inside:
            excluded(on, 5, "core-" + tool)
        else:
            assert row["status"] in (pj.COUNTED, pj.NOT_ELIGIBLE) and on["assessed"] == 1, (case.id, row)
            if not case.tool and case.shell == tool and not _platform_skips(case, tree):
                # the table's own expectation is the oracle for this row
                want = pj.COUNTED if case.reason == table.OK else pj.NOT_ELIGIBLE
                assert row["status"] == want, (case.id, case.attack, row)
                if want == pj.NOT_ELIGIBLE:
                    assert row["error_type"] == case.reason, (case.id, row)
        kinds[(row["status"], row.get("error_type") or row.get("exit_code") or "")] += 1
    assert sent > 900 and sum(kinds.values()) == asked
    if profile == tp.INTERACTIVE:
        assert kinds[(pj.COUNTED, "")] >= 1
    if profile == tp.STRICT:
        assert set(kinds) == {(pj.EXCLUDED, 2)} and asked > 900
    with capsys.disabled():
        print(f"\nDOOR {profile}: rows sent off and on={sent} asked={asked} "
              f"not sent={sum(skipped.values())} observation rows={sum(kinds.values())}")
        print("  by kind: " + ", ".join(f"{k[0]}:{k[1]}={v}" if k[1] != "" else f"{k[0]}={v}"
                                        for k, v in sorted(kinds.items(), key=str)))
