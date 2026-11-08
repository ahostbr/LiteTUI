"""T0306 — the device settings file is Owner's: only Owner's own seat may write it.

A data root's settings.json holds the paths of the programs LiteTUI launches,
the dispatch route, the API keys, and the DEFAULT allow and deny rules and tool
profile a new conversation starts from, and every seat on that data root shares
the one file. These arms hold the ownership refusal at the shared
_authorize_action door, beside the jobs.json one (T1085). What is refused is a
write that NAMES that file.

The target: a file whose name Windows opens as settings.json
(deny_floor.canonical_name, so a stream suffix and a trailing dot or space are
the same file) in a folder that holds `src/litetui`, or `.litetui-data.json`, or
is $LITETUI_DATA_ROOT (the floor's own folder test).

The bound for a shell command is the floor's, asked with this name. In the
floor's own words: "A redirect, tee / Out-File / Set-Content / Add-Content /
New-Item, a copy ONTO it, any move or rename of it, a delete, an editor, sed -i
or `python -c` are writes; a pure reader (cat, type, Get-Content, rg, a copy
FROM it, ...) is not." And its ceiling: "a path in a variable, a write from
inside a script file or through a reader's own exec feature, an 8.3 short name,
an admin share or a \\\\?\\ prefix, a link made to [the file] BEFORE this rule was
live, a data root known only to another process's environment, and writers that
are neither hooked agents nor LiteTUI seats".

Every test of the schedule-file rule (tests/test_jobs_file_t1085.py) is run a
second time here under a mirror: whatever it asks its rule is also asked of the
settings rule, the name substituted, and asked once more with the file moved
below `.agents` (an agent's home settings file in the same data root). At this commit 107 of those 111 tests ask
their rule something and so are mirrored; 4 ask it nothing and pass whatever the
settings rule does (named at ASKS_NOTHING, and pinned). A case added to that
file later is mirrored without an edit here.

ADDED (T0306-S, cut S1): the rule asks the floor with every recognition it can
add (deny_floor.MORE), so these are refused too: a copy, move or link of a file
of that name INTO the folder (`cp other/settings.json <root>/`, robocopy and
rsync included); a stream suffix on a FOLDER of a shell path word
(`<root>::$INDEX_ALLOCATION/settings.json`); the file's own 8.3 short name in a
writer's argument; `dd of=<file>`; `cp -l <file> alias`; Git pathspec magic
(`:/settings.json`, `:(top)settings.json`); the path inside a .NET call
(`[IO.File]::WriteAllText(...)`). The rule also reads the folder keys `workdir`,
`working_directory` and `dir` beside `cwd`, and tries a string under ANY key as
a path when its last segment is the name. And the data root's marker,
`.litetui-data.json`, is guarded the same way (a root known only by its marker
loses every guard once the marker is gone).

NOT covered, by this rule or by the schedule file's (they share every one):
- everything in the ceiling above, except the short name and the folder stream
  suffix, which the added recognitions now judge;
- a wildcard or a brace in the name of the file WRITTEN (`settings.jso?`,
  `*.json`, `{settings,x}.json`); a wildcard in a copy's SOURCE is judged;
- a whole-folder copy, extract or clean that never names the file;
- a .NET call on a reader's result (`(Get-Item <file>).Delete()`), and a `file:`
  URI under any key;
- a folder key and a file-name key given apart (`output_dir` + `filename`);
- a seat editing the installed source; the Claude Code hook path, which does not
  judge this file at all (only a LiteTUI seat does).
A delete or a garbled file is not a lesser case of those: the file then loads as
its defaults, which are the autonomous profile with no deny rules.
The rule brings the device settings.json level with jobs.json. It does not make
a seat a sandbox.

THE FILES BELOW `.agents` (the second half of this file). The same rule, the
same loop and the same recognizer also refuse a write that names ANY
settings.json at any depth below the data root's `.agents` folder:
  <root>/.agents/<Name>/conversations/<id>/settings.json   a conversation's own
      tool profile, allow and deny rules, hosts and program paths;
  <root>/.agents/<Name>/settings.json                      an agent's home file:
      its identity and the backend, model and thinking level it starts with.
A seat that is not the owner's is refused for its OWN conversation's file too;
it saves that file in-process (convo_settings.save, the agent's execution
update), which never passes the door. "Any depth" over-blocks any other
settings.json a tool might write below `.agents`; that is fail-safe.

When such a file takes effect, MEASURED on temp roots (the tests named
`test_measured_*`), because the limits below are worded from it:
- at an open or a resume, everything in it: the profile, the allow list, hosts;
- in a RUNNING conversation, the profile, when the file is next re-read; the
  conversation's chosen profile, which a typed turn carries, is then what the
  file says. The settings dialog re-reads the file after ANY save it makes, a
  device setting included. A command that saves a setting re-reads it only
  after it saves a conversation setting. (By reading, not run: the sidecar's
  settings patch and /subagent call the function the dialog calls.) The allow
  list in force is not re-read by either;
- in a running conversation, at a reconnect, the hosts, the skill roots and the
  other reconnect-time keys;
- a save from the seat's memory (a profile or model change in that seat)
  rewrites every known field and so erases a planted one;
- an agent's home file decides the backend and model at that agent's next
  launch; changed under a RUNNING seat it stops that seat's saves until it is
  reopened; garbled, it stops the agent from starting and, for as long as it
  is garbled, fails the ownership check of EVERY seat on the data root.
  THE RULE DOES NOT STAND BETWEEN A SEAT AND THAT STOP: an EMPTY FOLDER made
  below `.agents` stops every seat the same way, and the initializing marker
  put in an agent's home stops that ONE agent; neither names a settings file,
  and the rule says nothing about either (the catalog reads every folder below
  `.agents`);
- the legacy copy, <root>/.convos/<id>/settings.json, is never loaded: a seat
  refuses to resume a transcript outside its own agent. It is not in the rule.

NOT covered for the files below `.agents`, beyond everything listed above:
- a delete of the conversation's or the agent's whole folder, or of the file
  through a wildcard. A conversation whose file is missing or unreadable falls
  back to the device defaults, which are the autonomous profile, AND loses
  the fact that it was born in a spawned seat: resumed without the spawn
  marker it is no longer counted as spawned;
- the transcript (convo.jsonl), the catalog and an agent's memory files, which
  lie in the same tree and are other files.
"""
from __future__ import annotations

import json
import re
import types
from dataclasses import asdict

import pytest

import test_jobs_file_t1085 as jobs_tests

from litetui import agent_ownership, agent_store, claude_tools, convo_settings, seat_authority
from litetui import settings_runtime, tool_policy
from litetui import app as m
from litetui import settings as settings_mod
from litetui.settings_ui_adapter import SettingsUiAdapter
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, STRICT

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"
AGENT_ID = "11111111-1111-4111-8111-111111111111"
PROFILES = [STRICT, INTERACTIVE, AUTONOMOUS]


@pytest.fixture
def app(tmp_path):
    """A LiteTUI over an owned seat whose data root is tmp_path. The seat's own home
    settings (.agents/Probe/settings.json) sit inside that data root and are a
    different file from the device settings file."""
    home = tmp_path / ".agents" / "Probe"
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({
        "schema_version": 1, "name": "Probe", "agent_id": AGENT_ID,
        "execution": {"backend": "codex", "model": "fixture", "thinking_level": "high"},
    }), encoding="utf-8")
    with agent_ownership.AgentSession.acquire_existing(
            agent_store.AgentStore(tmp_path), agent_id=AGENT_ID) as session:
        yield m.LiteTUI(agent_session=session)


def _agent(a, monkeypatch, profile=AUTONOMOUS):
    """Make `a` a seat that is not the owner's; no human door and no rpc host may be asked."""
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    a._spawned_seat = True
    a._owner_seat = False
    a._spawner_id = SPAWNER
    sent: list = []
    a.seat.registered = True
    a.seat.send = lambda to, body: sent.append((to, body)) or False
    a._system = lambda *_a, **_k: None

    def no_door(*_a, **_k):
        raise AssertionError("a human door opened; the settings-file rule is not theirs to waive")

    monkeypatch.setattr(m, "show_dialog", no_door)

    async def no_rpc(*_a, **_k):
        raise AssertionError("the rpc host was asked")

    monkeypatch.setattr(m.tool_approval, "approve_over_rpc", no_rpc)
    return a, sent


def _owners(a):
    a._spawned_seat, a._owner_seat, a._pty_term = False, True, None
    return a


def _calls(target):
    """Every backend's spelling of a write or an edit of the settings file."""
    write_policy, write_args = claude_tools.native_policy(
        "Write", {"file_path": str(target), "content": "{}"})
    edit_policy, edit_args = claude_tools.native_policy(
        "Edit", {"file_path": str(target), "old_string": "{}", "new_string": "{ }"})
    patch = f"*** Begin Patch\n*** Update File: {target}\n@@\n-{{}}\n+{{ }}\n*** End Patch\n"
    return [
        ("write", {"path": str(target), "content": "{}"}, tool_policy.WRITE_POLICY),
        ("edit", {"path": str(target), "old_string": "{}", "new_string": "{ }"},
         tool_policy.WRITE_POLICY),
        ("Write", write_args, write_policy),
        ("Edit", edit_args, edit_policy),
        ("write", {"file_path": str(target), "content": "{}"}, tool_policy.WRITE_POLICY),
        ("apply_patch", {"command": ["apply_patch", patch]}, tool_policy.WRITE_POLICY),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("index", range(6))
async def test_a_seat_that_is_not_the_owners_may_not_write_the_settings_file(
        app, tmp_path, monkeypatch, profile, index):
    a, sent = _agent(app, monkeypatch, profile)
    name, args, policy = _calls(tmp_path / "settings.json")[index]
    result = await a._authorize_action(name, args, policy, workspace=tmp_path)
    assert result, (name, profile, "the write was allowed")
    text = result[0]
    assert "may not write" in text and "settings.json" in text and "T0306" in text, text
    assert "/settings" in text
    assert sent == [], "a settings.json write was relayed to the spawner for an APPROVE"
    assert a._active_tool_profile == profile, "the file guard must not change the profile"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
async def test_a_relative_path_is_judged_against_the_workspace(app, tmp_path, monkeypatch, profile):
    a, sent = _agent(app, monkeypatch, profile)
    result = await a._authorize_action("write", {"path": "settings.json", "content": "{}"},
                                       tool_policy.WRITE_POLICY, workspace=tmp_path)
    assert result and "T0306" in result[0]
    assert sent == []


@pytest.mark.parametrize("profile", PROFILES)
def test_the_owners_own_seat_is_not_refused_at_any_profile(app, tmp_path, profile):
    a = _owners(app)
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    for _name, args, policy in _calls(tmp_path / "settings.json"):
        assert seat_authority.settings_file_refusal(a, args, tmp_path, policy) is None, args


@pytest.mark.asyncio
async def test_the_owners_own_seat_passes_the_door(app, tmp_path, monkeypatch):
    a, sent = _agent(app, monkeypatch, AUTONOMOUS)
    _owners(a)
    name, args, policy = _calls(tmp_path / "settings.json")[0]
    assert await a._authorize_action(name, args, policy, workspace=tmp_path) is None
    assert sent == []


@pytest.mark.parametrize("clean", [True, False])
def test_a_protected_write_rechecks_owner_taint(app, tmp_path, monkeypatch, clean):
    """A cached owner mark must not exempt a bridge-tainted write mid-turn."""
    a = _owners(app)
    a._pty_term = "owner-terminal"
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term: clean)
    why = seat_authority.settings_file_refusal(
        a, {"path": str(tmp_path / "settings.json")}, tmp_path)
    assert (why is None) is clean
    assert a._owner_seat is clean


def test_an_unprotected_write_never_asks_who_owns_the_seat(app, tmp_path, monkeypatch):
    """The owner recheck costs a bridge call: only a recognized write pays it."""
    a = _owners(app)
    a._pty_term = "owner-terminal"

    def asked(term):
        raise AssertionError("the bridge was asked about a write that is not protected")

    monkeypatch.setattr(seat_authority, "pty_taint_clean", asked)
    monkeypatch.setattr(seat_authority, "pty_taint_state", asked)
    assert seat_authority.settings_file_refusal(
        a, {"path": str(tmp_path / "notes.md"), "content": "x"}, tmp_path) is None


def test_settings_save_works_in_a_seat_that_is_not_the_owners(app, tmp_path, monkeypatch):
    """`settings.save` is in-process and takes no part in the door, so the rule
    cannot fail this: it shows only that saving still works in such a seat. It
    does not show that the settings screen's path avoids the door."""
    a, _ = _agent(app, monkeypatch)
    written = settings_mod.save(a.settings, tmp_path)
    assert written == tmp_path / "settings.json" and written.is_file()


@pytest.mark.parametrize("alias", ["settings.json::$DATA", "settings.json.", "settings.json ",
                                   "settings.json:x", "SETTINGS.JSON"])
def test_a_windows_alias_of_the_name_is_refused_too(app, tmp_path, monkeypatch, alias):
    a, _ = _agent(app, monkeypatch)
    why = seat_authority.settings_file_refusal(
        a, {"path": str(tmp_path / "x")[:-1] + alias}, tmp_path)
    assert why and "T0306" in why, alias


def test_another_folders_settings_json_passes(app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch)
    for folder in ("elsewhere", ".vscode", "sub/.agents/Name"):
        other = tmp_path / folder
        other.mkdir(parents=True)
        assert seat_authority.settings_file_refusal(
            a, {"path": str(other / "settings.json"), "content": "{}"}, tmp_path) is None, folder


def test_a_data_root_is_known_by_its_marker_or_its_checkout(app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch)
    marked = tmp_path / "marked"
    marked.mkdir()
    (marked / ".litetui-data.json").write_text("{}")
    checkout = tmp_path / "checkout"
    (checkout / "src" / "litetui").mkdir(parents=True)
    for root in (marked, checkout):
        why = seat_authority.settings_file_refusal(
            a, {"path": str(root / "settings.json"), "content": "{}"}, tmp_path)
        assert why and "T0306" in why, root


@pytest.mark.parametrize("name", ["notes.md", "mysettings.json", "settings.json.bak",
                                  "settings.local.json", "app.settings.json"])
def test_other_files_in_the_data_root_pass(app, tmp_path, monkeypatch, name):
    a, _ = _agent(app, monkeypatch)
    assert seat_authority.settings_file_refusal(
        a, {"path": str(tmp_path / name), "content": "x"}, tmp_path) is None


@pytest.mark.asyncio
async def test_a_read_tool_path_is_not_a_write(app, tmp_path, monkeypatch):
    a, sent = _agent(app, monkeypatch)
    assert await a._authorize_action("read", {"path": str(tmp_path / "settings.json")},
                                     tool_policy.READ_POLICY, workspace=tmp_path) is None
    assert sent == []


# ── the shell forms: the floor's own recognizer, asked about this name ──────

JOBS, SETTINGS = "jobs.json", "settings.json"

SHELL_WRITERS = [
    ("bash", "echo x > {root}/NAME"),
    ("bash", "echo x >> {root}/NAME"),
    ("bash", "echo x | tee {root}/NAME"),
    ("powershell", '"x" | Out-File {root}/NAME'),
    ("powershell", "Set-Content {root}/NAME x"),
    ("powershell", "Add-Content {root}/NAME x"),
    ("powershell", "New-Item {root}/NAME -Force"),
    ("bash", "cp backup.json {root}/NAME"),
    ("powershell", "Copy-Item backup.json {root}/NAME"),
    ("powershell", "Copy-Item -Path backup.json -Destination {root}/NAME"),
    ("bash", "mv {root}/NAME old.json"),
    ("powershell", "Move-Item {root}/NAME old.json"),
    ("powershell", "Rename-Item {root}/NAME old.json"),
    ("bash", "rm {root}/NAME"),
    ("powershell", "Remove-Item {root}/NAME"),
    ("bash", "sed -i s/a/b/ {root}/NAME"),
    # Code handed to an interpreter: the floor rebuilds the path from the name it
    # was asked about. With that line asking about the schedule file's name
    # instead, these two rows are the ones that fail.
    ("bash", "python -c \"open('{root}/NAME','w').write('x')\""),
    ("bash", "node -e \"require('fs').writeFileSync('{root}/NAME','x')\""),
    ("bash", "git restore -- {root}/NAME"),
    ("bash", "git checkout HEAD -- {root}/NAME"),
    ("bash", "git rm -- {root}/NAME"),
    ("bash", "git mv -- {root}/NAME backup.json"),
    ("bash", "git clean -fx -- {root}/NAME"),
]
SHELL_READERS = [
    ("bash", "cat {root}/NAME"),
    ("powershell", "Get-Content {root}/NAME"),
    ("bash", "rg allow {root}/NAME"),
    ("bash", "cp {root}/NAME backup.json"),
    ("powershell", "Copy-Item -Destination backup.json -Path {root}/NAME"),
    ("bash", "git diff -- {root}/NAME"),
    ("bash", "git log -- {root}/NAME"),
    ("bash", "git show HEAD:{root}/NAME"),
]


async def _door(a, shell, command, workspace):
    return await a._authorize_action(shell, {"command": command}, tool_policy.SHELL_POLICY,
                                     workspace=workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("shell, template", SHELL_WRITERS)
async def test_a_shell_write_is_refused_as_the_schedule_files_is(
        app, tmp_path, monkeypatch, profile, shell, template):
    a, sent = _agent(app, monkeypatch, profile)
    command = template.format(root=tmp_path.as_posix())
    mine = await _door(a, shell, command.replace("NAME", SETTINGS), tmp_path)
    theirs = await _door(a, shell, command.replace("NAME", JOBS), tmp_path)
    assert mine and theirs, (command, mine, theirs)
    assert "T0306" in mine[0] and "may not" in mine[0] and SETTINGS in mine[0], mine[0]
    assert JOBS not in mine[0], "the settings refusal named the schedule file"
    assert sent == [], "a settings.json write was relayed to the spawner for an APPROVE"
    assert seat_authority.settings_file_refusal(
        _owners(a), {"command": command.replace("NAME", SETTINGS)}, tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("shell, template", SHELL_READERS)
async def test_a_shell_reader_stays_allowed(app, tmp_path, monkeypatch, shell, template):
    a, sent = _agent(app, monkeypatch)
    command = template.format(root=tmp_path.as_posix())
    for name in (SETTINGS, JOBS):
        assert await _door(a, shell, command.replace("NAME", name), tmp_path) is None, (name, command)
    assert sent == []


@pytest.mark.parametrize("target", ["mysettings.json", "settings.json.bak", "app.settings.json",
                                    ".vscode/settings.json", "sub/.agents/Probe/settings.json"])
def test_a_shell_write_to_a_look_alike_passes(app, tmp_path, monkeypatch, target):
    a, _ = _agent(app, monkeypatch)
    (tmp_path / ".vscode").mkdir()
    for command in (f"echo x > {tmp_path.as_posix()}/{target}", f"rm {tmp_path.as_posix()}/{target}"):
        assert seat_authority.settings_file_refusal(a, {"command": command}, tmp_path) is None, command


# A spelling the floor decodes before it matches: a rename of the name in the raw
# command text would miss every one of these (measured, 8 of 8).
DECODED = [
    ("bash empty quote pair", 'rm {root}/{a}""{b}'),
    ("bash single quote pair", "rm {root}/{a}''{b}"),
    ("bash quoted tail", 'rm {root}/{stem}."json"'),
    ("bash backslash", "rm {root}/{a}\\{b}"),
    ("bash ANSI-C hex", "rm $'{root}/\\x{first:02x}{rest}'"),
    ("redirect onto a quote pair", 'echo x > {root}/{a}""{b}'),
    ("powershell backtick", "Set-Content {root}/{a}`{b} x"),
    ("cmd caret", "del {native}\\{a}^{b}"),
]


def _spelled(template, name, root):
    return template.format(root=root.as_posix(), native=str(root), a=name[:2], b=name[2:],
                           stem=name.rsplit(".", 1)[0], first=ord(name[0]), rest=name[1:])


@pytest.mark.parametrize("label, template", DECODED)
def test_a_decoded_spelling_is_refused_as_the_schedule_files_is(
        app, tmp_path, monkeypatch, label, template):
    a, _ = _agent(app, monkeypatch)
    mine = _spelled(template, SETTINGS, tmp_path)
    assert SETTINGS not in mine, "CONTROL: the name must not be contiguous in the raw text"
    assert seat_authority.jobs_file_refusal(a, {"command": _spelled(template, JOBS, tmp_path)}, tmp_path)
    why = seat_authority.settings_file_refusal(a, {"command": mine}, tmp_path)
    assert why and "T0306" in why, (label, mine)


@pytest.mark.asyncio
@pytest.mark.parametrize("command, written", [
    (f"cp {SETTINGS} {JOBS}", JOBS),
    (f"cp {JOBS} {SETTINGS}", SETTINGS),
    (f"cat {JOBS} > {SETTINGS}", SETTINGS),
    (f"cat {SETTINGS} > {JOBS}", JOBS),
])
async def test_a_command_naming_both_files_is_refused_for_the_one_it_writes(
        app, tmp_path, monkeypatch, command, written):
    a, sent = _agent(app, monkeypatch)
    rules = {JOBS: seat_authority.jobs_file_refusal, SETTINGS: seat_authority.settings_file_refusal}
    read = SETTINGS if written == JOBS else JOBS
    assert rules[read](a, {"command": command}, tmp_path) is None, "the file it only reads refused it"
    why = rules[written](a, {"command": command}, tmp_path)
    assert why and str(tmp_path / written) in why, why
    denied = await _door(a, "bash", command, tmp_path)
    assert denied and str(tmp_path / written) in denied[0] and str(tmp_path / read) not in denied[0]
    assert sent == []


@pytest.mark.parametrize("name, other", [(SETTINGS, JOBS), (JOBS, SETTINGS)])
def test_a_folder_whose_name_holds_the_text_is_judged_as_the_folder_it_is(
        app, tmp_path, monkeypatch, name, other):
    """The folder test runs on the folder the command names, whatever that
    folder is called: a data root under a folder named with the text is
    protected, and a plain folder is not, even beside a data root whose path
    differs only by the other file's name."""
    a, _ = _agent(app, monkeypatch)
    rule = {JOBS: seat_authority.jobs_file_refusal, SETTINGS: seat_authority.settings_file_refusal}[name]
    under = tmp_path / "a" / f"{name}.d"
    under.mkdir(parents=True)
    (under / ".litetui-data.json").write_text("{}")
    assert rule(a, {"command": f"echo x > {under.as_posix()}/{name}"}, tmp_path)
    plain = tmp_path / "b" / f"{name}.d"
    plain.mkdir(parents=True)
    twin = tmp_path / "b" / f"{other}.d"
    twin.mkdir()
    (twin / ".litetui-data.json").write_text("{}")
    assert rule(a, {"command": f"echo x > {plain.as_posix()}/{name}"}, tmp_path) is None


# ── every settings.json below the data root's `.agents` folder: a conversation's
#    own file and an agent's home file. The same rule, asked a second time inside
#    the same loop with the folder the file may sit below. ──

PLACES = ["this seat's own conversation", "another agent's conversation",
          "this agent's home", "another agent's home"]
BELOW_WORDS = "a conversation's or an agent's own settings"
DEVICE_WORDS = "dispatch routes"


def _place(a, root, place):
    """The settings file of `place`. Only this agent's home file is on disk."""
    return {
        PLACES[0]: a.convo_dir,
        PLACES[1]: root / ".agents" / "Other" / "conversations" / "c1",
        PLACES[2]: root / ".agents" / "Probe",
        PLACES[3]: root / ".agents" / "Other",
    }[place] / SETTINGS


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("index", range(6))
async def test_a_seat_that_is_not_the_owners_may_not_write_a_settings_file_below_agents(
        app, tmp_path, monkeypatch, profile, place, index):
    a, sent = _agent(app, monkeypatch, profile)
    target = _place(a, tmp_path, place)
    name, args, policy = _calls(target)[index]
    result = await a._authorize_action(name, args, policy, workspace=tmp_path)
    assert result, (name, profile, place, "the write was allowed")
    text = result[0]
    assert "may not write" in text and str(target) in text and "T0306" in text, text
    assert BELOW_WORDS in text and DEVICE_WORDS not in text, "the device file's words were used"
    assert "/settings" in text
    assert sent == [], "the write was relayed to the spawner for an APPROVE"
    assert a._active_tool_profile == profile, "the file guard must not change the profile"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("place", [PLACES[0], PLACES[3]])
@pytest.mark.parametrize("shell, template", SHELL_WRITERS)
async def test_a_shell_write_below_agents_is_refused(
        app, tmp_path, monkeypatch, profile, place, shell, template):
    a, sent = _agent(app, monkeypatch, profile)
    target = _place(a, tmp_path, place)
    command = template.format(root=target.parent.as_posix()).replace("NAME", SETTINGS)
    denied = await _door(a, shell, command, tmp_path)
    assert denied, (command, "the write was allowed")
    assert "T0306" in denied[0] and "may not" in denied[0] and BELOW_WORDS in denied[0], denied[0]
    assert JOBS not in denied[0] and DEVICE_WORDS not in denied[0], denied[0]
    # A Git write is refused by the floor's Git question, which names no path.
    said = "run a Git command that writes" if command.startswith("git ") else f"write {target}"
    assert said in denied[0], denied[0]
    assert sent == [], "the write was relayed to the spawner for an APPROVE"
    # CONTROL: the schedule file's rule is asked about the data root itself only.
    assert seat_authority.jobs_file_refusal(
        a, {"command": command.replace(SETTINGS, JOBS)}, tmp_path) is None
    assert seat_authority.settings_file_refusal(_owners(a), {"command": command}, tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
async def test_a_relative_path_below_agents_is_judged_against_its_folder(
        app, tmp_path, monkeypatch, profile):
    a, sent = _agent(app, monkeypatch, profile)
    a.convo_dir.mkdir(parents=True)
    (tmp_path / "work").mkdir()
    # The rule also tries the process's own folder as a base, and the tests run
    # from a checkout, which is a data root: stand somewhere that is not one.
    monkeypatch.chdir(tmp_path / "work")
    by_workspace = await a._authorize_action("write", {"path": SETTINGS, "content": "{}"},
                                             tool_policy.WRITE_POLICY, workspace=a.convo_dir)
    by_cwd = await a._authorize_action(
        "bash", {"command": f"echo x > {SETTINGS}", "cwd": str(a.convo_dir)},
        tool_policy.SHELL_POLICY, workspace=tmp_path / "work")
    by_cd = await _door(a, "bash", f"cd {a.convo_dir.as_posix()} && echo x > {SETTINGS}", tmp_path / "work")
    for result in (by_workspace, by_cwd, by_cd):
        assert result and BELOW_WORDS in result[0], result
    assert sent == []


@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("place", PLACES)
def test_the_owners_own_seat_is_not_refused_below_agents(app, tmp_path, place, profile):
    a = _owners(app)
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    for _name, args, policy in _calls(_place(a, tmp_path, place)):
        assert seat_authority.settings_file_refusal(a, args, tmp_path, policy) is None, args


@pytest.mark.asyncio
@pytest.mark.parametrize("place", [PLACES[0], PLACES[3]])
async def test_the_owners_own_seat_passes_the_door_below_agents(app, tmp_path, monkeypatch, place):
    a, sent = _agent(app, monkeypatch, AUTONOMOUS)
    _owners(a)
    name, args, policy = _calls(_place(a, tmp_path, place))[0]
    assert await a._authorize_action(name, args, policy, workspace=tmp_path) is None
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("place", [PLACES[0], PLACES[3]])
@pytest.mark.parametrize("shell, template", SHELL_READERS)
async def test_a_reader_below_agents_stays_allowed(app, tmp_path, monkeypatch, place, shell, template):
    a, sent = _agent(app, monkeypatch)
    target = _place(a, tmp_path, place)
    command = template.format(root=target.parent.as_posix()).replace("NAME", SETTINGS)
    assert await _door(a, shell, command, tmp_path) is None, command
    assert await a._authorize_action("read", {"path": str(target)}, tool_policy.READ_POLICY,
                                     workspace=tmp_path) is None
    assert sent == []


@pytest.mark.parametrize("folder", ["plain/.agents/Seat", "plain/.agents/Seat/conversations/c1",
                                    "sub/.agents/Seat", ".agents2/Seat", "agents/Seat", "x.agents/Seat"])
def test_a_settings_json_below_another_folder_passes(app, tmp_path, monkeypatch, folder):
    """`.agents` of a folder that is not a data root (plain/, sub/: no marker, no
    checkout, not the environment's root), and a folder of another name."""
    a, _ = _agent(app, monkeypatch)
    target = tmp_path / folder / SETTINGS
    for args in ({"path": str(target), "content": "{}"},
                 {"command": f"echo x > {target.as_posix()}"}, {"command": f"rm {target.as_posix()}"}):
        assert seat_authority.settings_file_refusal(a, args, tmp_path) is None, args


@pytest.mark.parametrize("name", ["convo.jsonl", "memory.md", "memories/note.md", "soul.md", "handoff.md",
                                  "settings.json.bak", "mysettings.json", "settings.local.json"])
def test_other_files_below_agents_pass(app, tmp_path, monkeypatch, name):
    """The transcript, the memory files and look-alike names are other files:
    this rule says nothing about them (and so does not protect them)."""
    a, _ = _agent(app, monkeypatch)
    for folder in (a.convo_dir, tmp_path / ".agents" / "Probe"):
        target = folder / name
        for args in ({"path": str(target), "content": "x"},
                     {"command": f"echo x > {target.as_posix()}"}, {"command": f"rm {target.as_posix()}"}):
            assert seat_authority.settings_file_refusal(a, args, tmp_path) is None, args


@pytest.mark.parametrize("where", ["one folder below a conversation's folder",
                                   "a second .agents inside an agent's home",
                                   "a data root kept below another .agents folder"])
def test_any_depth_and_every_agents_folder_on_the_path(app, tmp_path, monkeypatch, where):
    """ANY depth: a file deeper than a conversation's own, the deepest that
    exists today. And EVERY `.agents` on the path is tried: with a second one
    inside a home the outer one is the data root's; with a data root kept below
    another `.agents` the inner one is."""
    a, _ = _agent(app, monkeypatch)
    kept = tmp_path / "plain" / ".agents" / "kept"
    kept.mkdir(parents=True)
    (kept / ".litetui-data.json").write_text("{}", encoding="utf-8")
    target = {
        "one folder below a conversation's folder": a.convo_dir / "attachments" / SETTINGS,
        "a second .agents inside an agent's home": tmp_path / ".agents" / "Probe" / ".agents" / "x" / SETTINGS,
        "a data root kept below another .agents folder": kept / ".agents" / "Seat" / SETTINGS,
    }[where]
    for args in ({"path": str(target), "content": "{}"},
                 {"command": f"echo x > {target.as_posix()}"}, {"command": f"rm {target.as_posix()}"}):
        why = seat_authority.settings_file_refusal(a, args, tmp_path)
        assert why and BELOW_WORDS in why and "T0306" in why, args
    # CONTROL: two `.agents` on the path and no data root directly above either.
    neither = tmp_path / "plain" / ".agents" / "other" / ".agents" / "Seat" / SETTINGS
    assert seat_authority.settings_file_refusal(a, {"path": str(neither), "content": "{}"}, tmp_path) is None


def test_a_relative_path_is_judged_from_the_processs_own_folder_below_agents(
        app, tmp_path, monkeypatch):
    """The rule tries the process's own folder as a base, as the schedule-file
    rule does. Standing inside the tree with the workspace elsewhere, a bare
    relative name is the conversation's own file."""
    a, _ = _agent(app, monkeypatch)
    a.convo_dir.mkdir(parents=True)
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    for args in ({"path": SETTINGS, "content": "{}"}, {"command": f"echo x > {SETTINGS}"},
                 {"command": f"rm {SETTINGS}"}):
        assert seat_authority.settings_file_refusal(a, args, tmp_path / "work") is None, (
            "CONTROL: standing in a plain folder this is no protected file", args)
    monkeypatch.chdir(a.convo_dir)
    for args in ({"path": SETTINGS, "content": "{}"}, {"command": f"echo x > {SETTINGS}"},
                 {"command": f"rm {SETTINGS}"}):
        why = seat_authority.settings_file_refusal(a, args, tmp_path / "work")
        assert why and BELOW_WORDS in why and str(a.convo_dir / SETTINGS) in why, args


@pytest.mark.parametrize("folder", [".AGENTS", ".agents.", ".agents::$INDEX_ALLOCATION",
                                    ".agents:$I30:$INDEX_ALLOCATION"])
@pytest.mark.parametrize("name", [SETTINGS, "settings.json::$DATA", "SETTINGS.JSON", "settings.json."])
def test_a_windows_alias_of_the_folder_or_the_name_is_refused_by_the_write_tool(
        app, tmp_path, monkeypatch, folder, name):
    """Windows opens each of these as the agent's home file (the folder aliases
    measured on NTFS). The write and edit tools resolve the path themselves, so
    the stream suffix on the FOLDER, which a shell command is not judged for
    (see the header), is judged here."""
    a, _ = _agent(app, monkeypatch)
    spelled = f"{tmp_path}\\{folder}\\Probe\\{name}"
    why = seat_authority.settings_file_refusal(a, {"path": spelled, "content": "{}"}, tmp_path)
    assert why and BELOW_WORDS in why, spelled


def test_the_folders_short_name_is_followed_when_the_volume_gives_one(app, tmp_path, monkeypatch):
    import ctypes
    import os
    if os.name != "nt":
        pytest.skip("short names are a Windows volume feature")
    buffer = ctypes.create_unicode_buffer(600)
    ctypes.windll.kernel32.GetShortPathNameW(str(tmp_path / ".agents"), buffer, 600)
    short = buffer.value.replace("\\", "/").rsplit("/", 1)[-1]
    if not short or short.lower() == ".agents":
        pytest.skip("this volume gives the folder no short name")
    a, _ = _agent(app, monkeypatch)
    spelled = f"{tmp_path.as_posix()}/{short}/Probe/{SETTINGS}"
    for args in ({"path": spelled, "content": "{}"}, {"command": f"echo x > {spelled}"},
                 {"command": f"rm {spelled}"}):
        why = seat_authority.settings_file_refusal(a, args, tmp_path)
        assert why and BELOW_WORDS in why, args


def test_a_same_named_file_copied_into_the_folder_is_refused(app, tmp_path, monkeypatch):
    """T0306-S closes what was pinned here as a known limit. A copy or a move
    whose destination is the FOLDER carries the file's name in its source word
    only; the floor's added recognition (copy-into) judges folder / the source's
    name. For a file below `.agents`, for the device file and for the schedule
    file alike; and only for a seat that is not the owner's."""
    a, _ = _agent(app, monkeypatch)
    a.convo_dir.mkdir(parents=True)
    forms = ("cp other/NAME {dir}/", "mv other/NAME {dir}", "cp -t {dir} other/NAME",
             "Copy-Item other/NAME -Destination {dir}", "cd {dir} && cp ../other/NAME .")
    asked = [(SETTINGS, seat_authority.settings_file_refusal, folder)
             for folder in (tmp_path, a.convo_dir, tmp_path / ".agents" / "Probe")]
    asked.append((JOBS, seat_authority.jobs_file_refusal, tmp_path))
    for name, rule, folder in asked:
        assert rule(a, {"command": f"cp other/{name} {folder.as_posix()}/{name}"}, tmp_path)
        for form in forms:
            command = form.format(dir=folder.as_posix()).replace("NAME", name)
            why = rule(a, {"command": command}, tmp_path)
            assert why and "T0306" in why or "T1085" in (why or ""), command
            assert rule(_owners(a), {"command": command}, tmp_path) is None, "the owner's own seat"
            a._spawned_seat, a._owner_seat = True, False
        # CONTROL: a file of another name copied into the folder is not this file.
        assert rule(a, {"command": f"cp other/notes.md {folder.as_posix()}/"}, tmp_path) is None


# ── T0306-S: the floor's added recognitions, asked by the rule (MORE) ──

#: (command, the file it writes): ROOT a data root, OTHER a plain folder, NAME the file.
MORE_WRITES = [
    "cp OTHER/NAME ROOT/", "mv OTHER/NAME ROOT",
    "cp --target=ROOT OTHER/NAME", "cp -tROOT OTHER/NAME", "env cp OTHER/NAME ROOT/",
    "timeout 20 cp OTHER/NAME ROOT/", "Copy-Item OTHER/NAME,OTHER/x.json ROOT",
    "cp --li ROOT/NAME OTHER/alias",
    'echo x > "ROOT::$INDEX_ALLOCATION/NAME"', 'rm "ROOT:$I30:$INDEX_ALLOCATION/NAME"',
    "dd if=/dev/null of=ROOT/NAME", "cp -l ROOT/NAME OTHER/alias", "cp --link ROOT/NAME OTHER/alias",
    "[IO.File]::WriteAllText('ROOT/NAME','x')", '[System.IO.File]::Delete("ROOT/NAME")',
]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize("template", MORE_WRITES)
async def test_an_added_recognition_is_refused_for_a_seat_that_is_not_the_owners(
        app, tmp_path, monkeypatch, profile, template):
    a, sent = _agent(app, monkeypatch, profile)
    other = tmp_path / "other"
    other.mkdir()
    for name, rule in ((SETTINGS, seat_authority.settings_file_refusal), (JOBS, seat_authority.jobs_file_refusal)):
        command = template.replace("ROOT", tmp_path.as_posix()).replace("OTHER", other.as_posix()).replace("NAME", name)
        denied = await _door(a, "bash", command, tmp_path)
        assert denied and "may not" in denied[0], (command, "the write was allowed")
        assert rule(a, {"command": command}, tmp_path), command
        assert rule(_owners(a), {"command": command}, tmp_path) is None, "the owner's own seat"
        a._spawned_seat, a._owner_seat = True, False
    assert sent == []


def test_the_short_name_and_the_pathspec_are_refused(app, tmp_path, monkeypatch):
    """H, on a file that exists (the volume must give it a short name), and N
    from a subfolder of a repository whose root is the data root."""
    import ctypes
    import os
    a, _ = _agent(app, monkeypatch)
    (tmp_path / ".git").mkdir()
    (tmp_path / "sub").mkdir()
    for name, rule in ((SETTINGS, seat_authority.settings_file_refusal), (JOBS, seat_authority.jobs_file_refusal)):
        for command in (f"git checkout HEAD -- :/{name}", f"git checkout HEAD -- ':(top){name}'"):
            why = rule(a, {"command": command, "cwd": "sub"}, tmp_path)
            assert why, command
            if name == SETTINGS:   # the floor's Git question answered it, not only the shell's
                assert "run a Git command that writes" in why, why
        if os.name != "nt":
            continue
        (tmp_path / name).write_text("{}", encoding="utf-8")
        buffer = ctypes.create_unicode_buffer(600)
        ctypes.windll.kernel32.GetShortPathNameW(str(tmp_path / name), buffer, 600)
        short = buffer.value.replace("\\", "/").rsplit("/", 1)[-1]
        if short and short.lower() != name:
            assert rule(a, {"command": f"rm {tmp_path.as_posix()}/{short}"}, tmp_path), short
            assert rule(a, {"command": f"cat {tmp_path.as_posix()}/{short}"}, tmp_path) is None


# ── T0306-S F: more folder keys, and a string under ANY key tried as a path ──

@pytest.mark.parametrize("key", ["workdir", "working_directory", "dir", "cwd"])
def test_a_folder_key_is_a_base(app, tmp_path, monkeypatch, key):
    a, _ = _agent(app, monkeypatch)
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    args = {"cmd": f"echo x > {SETTINGS}", "command": f"echo x > {SETTINGS}", key: str(tmp_path)}
    assert seat_authority.settings_file_refusal(a, args, tmp_path / "work"), key
    assert seat_authority.settings_file_refusal(a, {**args, key: str(tmp_path / "work")},
                                                tmp_path / "work") is None, "CONTROL: a plain folder"


@pytest.mark.parametrize("args", [
    {"output_path": "ROOT/NAME"}, {"destination": "ROOT/NAME", "source": "x.json"},
    {"filePath": "ROOT/NAME", "content": "x"}, {"file": "ROOT/NAME"}, {"filename": "ROOT/NAME"},
    {"paths": ["x.md", "ROOT/NAME"]}, {"target_file": "ROOT/NAME"}, {"nested": {"deeper": "ROOT/NAME"}},
    {"target": "ROOT/agents-file/../NAME"},
])
def test_a_path_under_any_key_is_tried(app, tmp_path, monkeypatch, args):
    a, _ = _agent(app, monkeypatch)
    for name, rule in ((SETTINGS, seat_authority.settings_file_refusal), (JOBS, seat_authority.jobs_file_refusal)):
        spelled = json.loads(json.dumps(args).replace("ROOT", tmp_path.as_posix()).replace("NAME", name))
        assert rule(a, spelled, tmp_path), spelled
        plain = json.loads(json.dumps(args).replace("ROOT", (tmp_path / "plain").as_posix()).replace("NAME", name))
        assert rule(a, plain, tmp_path) is None, "CONTROL: not a data root"


def test_a_read_only_tool_with_any_key_stays_exempt(app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch)
    args = {"query": "x", "file": str(tmp_path / SETTINGS)}
    assert seat_authority.settings_file_refusal(a, args, tmp_path, tool_policy.READ_POLICY) is None
    assert seat_authority.settings_file_refusal(a, args, tmp_path, tool_policy.WRITE_POLICY)


def test_a_string_that_is_not_the_name_is_not_tried(app, tmp_path, monkeypatch):
    """Only a string whose last segment is the name is tried as a path, so
    content that mentions the file is not a write of it."""
    a, _ = _agent(app, monkeypatch)
    for content in (f"see {tmp_path.as_posix()}/{SETTINGS} for the rules", f"{SETTINGS} is the device file",
                    f"{tmp_path.as_posix()}/{SETTINGS}.bak"):
        assert seat_authority.settings_file_refusal(
            a, {"path": str(tmp_path / "notes.md"), "content": content}, tmp_path) is None, content


# ── T0306-S D: the data root's marker is asked about as a third name ──

MARKER = ".litetui-data.json"   # spelled here, not read from the rule: the rule's name is what is tested


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"command": "rm ROOT/MARKER"}, {"command": "Remove-Item ROOT/MARKER"},
    {"command": "echo x > ROOT/MARKER"}, {"command": "mv ROOT/MARKER ROOT/old.json"},
    {"path": "ROOT/MARKER", "content": "{}"},
])
async def test_the_data_roots_marker_is_refused_for_a_seat_that_is_not_the_owners(
        app, tmp_path, monkeypatch, args):
    a, sent = _agent(app, monkeypatch)
    (tmp_path / MARKER).write_text("{}", encoding="utf-8")
    spelled = {k: v.replace("ROOT", tmp_path.as_posix()).replace("MARKER", MARKER) for k, v in args.items()}
    why = seat_authority.settings_file_refusal(a, spelled, tmp_path)
    assert why and "T0306" in why and "marks the folder as a LiteTUI data root" in why, spelled
    policy = tool_policy.SHELL_POLICY if "command" in spelled else tool_policy.WRITE_POLICY
    assert await a._authorize_action("bash" if "command" in spelled else "write", spelled, policy,
                                     workspace=tmp_path)
    assert seat_authority.settings_file_refusal(_owners(a), spelled, tmp_path) is None, "the owner's own seat"
    assert sent == []


def test_a_marker_in_a_folder_that_is_no_data_root_passes(app, tmp_path, monkeypatch):
    """Planting a marker only ever over-blocks: a folder that is no data root
    yet may get one, and another file's name there is not the marker."""
    a, _ = _agent(app, monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    for args in ({"command": f"echo x > {plain.as_posix()}/{MARKER}"},
                 {"path": str(plain / MARKER), "content": "{}"},
                 {"command": f"rm {tmp_path.as_posix()}/{MARKER}.bak"}):
        assert seat_authority.settings_file_refusal(a, args, tmp_path) is None, args


# ── T0306-S fix 1 (review R2): the owner's own seat and a bridge that does not answer ──

#: (args, does the rule before T0306-S see it): ROOT the data root (the workspace).
OWNER_ROWS = [
    ({"command": "echo x > ROOT/settings.json"}, True),
    ({"path": "ROOT/settings.json", "content": "{}"}, True),
    ({"command": "cp OTHER/settings.json ROOT/"}, False),             # MORE: copy-into
    ({"command": "env cp OTHER/settings.json ROOT/"}, False),
    ({"path": "notes.md", "content": "settings.json"}, False),         # F: a value under any key
    ({"files": ["settings.json"]}, False),
    ({"query": ".litetui-data.json"}, False),                          # D: the marker
    ({"command": "rm ROOT/.litetui-data.json"}, False),                # D: named, but new in T0306-S
    ({"command": "echo x > ../settings.json", "workdir": "ROOT/sub"}, False),   # F: a folder key
    ({"command": "rm ROOT/.litetui-data.json ROOT/settings.json"}, True),
]


def _rows(tmp_path):
    other = tmp_path / "other"
    other.mkdir(exist_ok=True)
    for args, before in OWNER_ROWS:
        yield json.loads(json.dumps(args).replace("ROOT", tmp_path.as_posix())
                         .replace("OTHER", other.as_posix())), before


def _owner_in_a_terminal(a, monkeypatch, answer):
    """The owner's own seat in a LiteSuite terminal; the bridge answers `answer`
    (True tainted, False untainted, None no answer). Every recheck is counted."""
    _owners(a)
    a._pty_term = "owner-terminal"
    asked = []
    monkeypatch.setattr(seat_authority, "pty_taint_state",
                        lambda term, timeout=1.0: asked.append(term) or answer)
    monkeypatch.setattr(seat_authority, "pty_taint_clean",
                        lambda term, timeout=1.0: asked.append(term) or answer is False)
    return asked


def test_the_owners_seat_with_the_bridge_answering_is_not_refused(app, tmp_path, monkeypatch):
    a = app
    asked = _owner_in_a_terminal(a, monkeypatch, False)
    monkeypatch.chdir(tmp_path)
    for args, _ in _rows(tmp_path):
        assert seat_authority.settings_file_refusal(a, args, tmp_path) is None, args
        assert a._owner_seat is True
    assert len(asked) == len(OWNER_ROWS), "one recheck per recognized call"


def test_a_silent_bridge_refuses_the_call_and_demotes_only_for_what_the_rule_saw_before(
        app, tmp_path, monkeypatch):
    """Pick 2: only seats that are not the owner's are refused. A write only the
    added recognitions see may be refused while the bridge is silent (the
    fail-safe), but it must not clear the owner mark; a write the rule saw before
    clears it, as it always did."""
    a = app
    monkeypatch.chdir(tmp_path)
    for args, before in _rows(tmp_path):
        asked = _owner_in_a_terminal(a, monkeypatch, None)
        why = seat_authority.settings_file_refusal(a, args, tmp_path)
        assert why and "T0306" in why, (args, "refused while the bridge is silent")
        assert a._owner_seat is (not before), (args, "the owner mark")
        assert len(asked) == 1, (args, "one recheck for the call, the marker and the settings file together")


def test_a_tainted_terminal_still_clears_the_mark_for_an_added_recognition(app, tmp_path, monkeypatch):
    a = app
    _owner_in_a_terminal(a, monkeypatch, True)
    other = tmp_path / "other"
    other.mkdir()
    why = seat_authority.settings_file_refusal(
        a, {"command": f"cp {other.as_posix()}/settings.json {tmp_path.as_posix()}/"}, tmp_path)
    assert why and a._owner_seat is False


@pytest.mark.parametrize("reply, state", [
    (b'{"tainted": false}', False), (b'{"tainted": true}', True), (b'{}', None),
    (b'{"tainted": "no"}', None), (b'not json', None), (OSError("refused"), None),
])
def test_the_bridges_answer_is_true_false_or_none(monkeypatch, tmp_path, reply, state):
    """pty_taint_state tells "no answer" apart from "tainted"; pty_taint_clean is
    still True only for a clear untainted answer."""
    import io
    import urllib.request

    def urlopen(request, timeout):
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(reply)

    monkeypatch.setenv("LITESUITE_BRIDGE_TOKEN", "tok")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert seat_authority.pty_taint_state("pty-1-1") is state
    assert seat_authority.pty_taint_clean("pty-1-1") is (state is False)
    monkeypatch.delenv("LITESUITE_BRIDGE_TOKEN")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", staticmethod(lambda: tmp_path))   # no token file
    assert seat_authority.pty_taint_state("pty-1-1") is None


def test_another_seat_is_refused_every_row(app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch)
    monkeypatch.chdir(tmp_path)
    for args, _ in _rows(tmp_path):
        assert seat_authority.settings_file_refusal(a, args, tmp_path), args
    a._owner_seat, a._pty_term = True, None   # owner-marked, but spawned: still not the owner's
    for args, _ in _rows(tmp_path):
        assert seat_authority.settings_file_refusal(a, args, tmp_path), args


def _born(a):
    """Give `a` a conversation of its own on disk, as a new conversation gets."""
    a.convo_dir.mkdir(parents=True, exist_ok=True)
    a._adopt_convo_settings(born=True)
    return a.convo_dir / SETTINGS


def test_a_seat_that_is_not_the_owners_still_saves_its_own_settings_in_process(
        app, tmp_path, monkeypatch):
    """The in-process saves take no part in the door, so the rule cannot fail
    them: this shows only that each still WORKS in such a seat, and that the
    same seat is refused the same file through a tool. The three saves: the
    conversation file from the seat's memory, the app's own write-through of
    one field, and the agent's execution update of its home file."""
    a, _ = _agent(app, monkeypatch)
    session = a._agent_session
    try:
        mine = _born(a)
        assert json.loads(mine.read_text(encoding="utf-8"))["tool_policy_profile"] == AUTONOMOUS
        written = convo_settings.save(a.convo_dir, convo_settings.ConvoSettings(tool_policy_profile=STRICT),
                                      agent_session=session)
        assert written == mine and json.loads(mine.read_text(encoding="utf-8"))["tool_policy_profile"] == STRICT
        a._remember_for_this_convo("tool_policy_profile", INTERACTIVE)
        assert json.loads(mine.read_text(encoding="utf-8"))["tool_policy_profile"] == INTERACTIVE
        home = tmp_path / ".agents" / "Probe" / SETTINGS
        session.update_execution(backend="codex", model="fixture-2", thinking_level="high")
        assert json.loads(home.read_text(encoding="utf-8"))["execution"]["model"] == "fixture-2"
        for path in (mine, home):
            assert seat_authority.settings_file_refusal(a, {"path": str(path), "content": "{}"}, tmp_path)
    finally:
        a.store.release()


# ── MEASURED: when such a file takes effect. These do not test the rule; they pin
#    the facts the limits in the header are worded from. "Plant" = the file is
#    changed on disk behind the seat's back, as a tool write would change it. ──

PLANTED_ALLOW = ["shell(rm *)"]
PLANTED_HOST = "http://planted.invalid:1"


def _plant(path, **execution):
    """Plant the autonomous profile, and `execution` keys, in a conversation's file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tool_policy_profile"] = AUTONOMOUS
    raw["execution"]["tool_policy_profile"] = AUTONOMOUS
    raw["execution"].update(execution)
    path.write_text(json.dumps(raw), encoding="utf-8")


def _on_disk(path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw["tool_policy_profile"], raw["execution"].get("tool_always_allow"), raw["execution"].get("lm_host")


def test_measured_a_resume_adopts_the_planted_profile_and_allow_row(app, tmp_path, monkeypatch):
    """The real _resume, with only its redraw and its backend connect stubbed
    (neither reads the file; both need a mounted screen)."""
    a, _ = _agent(app, monkeypatch, STRICT)
    monkeypatch.setattr(m.LiteTUI, "_render_resumed", lambda self, path: None)
    monkeypatch.setattr(m.LiteTUI, "connect", lambda self, *_a, **_k: None)
    try:
        a._materialise_convo()
        a._append({"role": "user", "content": "saved"})
        assert a.chosen_tool_profile == STRICT and a.settings.tool_always_allow == []
        _plant(a.convo_dir / SETTINGS, tool_always_allow=PLANTED_ALLOW)
        assert a.chosen_tool_profile == STRICT, "CONTROL: a plant alone changes nothing in a running seat"
        assert a._resume(a.convo_path)
        assert a.chosen_tool_profile == AUTONOMOUS and a._active_tool_profile == AUTONOMOUS
        assert a.settings.tool_always_allow == PLANTED_ALLOW
    finally:
        a.store.release()


def test_measured_a_save_from_the_seats_memory_erases_a_plant(app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch, STRICT)
    try:
        path = _born(a)
        _plant(path, tool_always_allow=PLANTED_ALLOW, lm_host=PLANTED_HOST)
        assert _on_disk(path) == (AUTONOMOUS, PLANTED_ALLOW, PLANTED_HOST)
        a._remember_for_this_convo("seat_tier", "leader")     # any write-through of one field
        assert _on_disk(path) == (STRICT, [], a.settings.lm_host) and a.settings.lm_host != PLANTED_HOST
        assert a.chosen_tool_profile == STRICT
    finally:
        a.store.release()


def _settings_dialog(a):
    """The /settings dialog's own three bindings for an open conversation, as
    plugins/settings_ui builds them: the form is filled from the files on disk,
    a save goes to the settings service's patch, and what was saved is applied
    by settings_runtime.apply_saved_result."""
    service = settings_runtime.service_for(a)
    conversation_id = a.convo_dir.name
    return SettingsUiAdapter(
        a.settings,
        snapshot_provider=lambda: settings_runtime.snapshot_with_launch(a, conversation_id),
        save_patch=lambda changes, revisions: service.save_patch(conversation_id, changes, revisions),
        runtime_apply=lambda requested, result: settings_runtime.apply_saved_result(a, requested, result))


@pytest.mark.parametrize("by, saved, adopted", [
    ("the settings dialog", "a conversation setting", True),
    ("the settings dialog", "a device setting", True),
    ("a command that saves a setting", "a conversation setting", True),
    ("a command that saves a setting", "a device setting", False),
])
def test_measured_a_running_conversation_adopts_a_planted_profile_when_its_file_is_re_read(
        app, tmp_path, monkeypatch, by, saved, adopted):
    """One unrelated setting is saved in a running conversation whose file holds
    a planted profile. The dialog (on its own bindings) re-reads the
    conversation's file after ANY save, so even a device setting adopts the
    plant; a command re-reads it only after it saves a conversation setting.
    The plant stays in the file either way. Where it is adopted, the
    conversation's chosen profile, which a typed turn carries, is the planted
    one; the turn in flight and the allow list in force are not changed."""
    a, _ = _agent(app, monkeypatch, STRICT)
    field = "temperature" if saved == "a conversation setting" else "show_thinking"
    try:
        path = _born(a)
        _plant(path, tool_always_allow=PLANTED_ALLOW)
        if by == "the settings dialog":
            dialog = _settings_dialog(a)
            form = dialog.effective
            assert form.tool_policy_profile == AUTONOMOUS and form.tool_always_allow == PLANTED_ALLOW, (
                "the form is filled from the file")
            setattr(form, field, 0.5 if field == "temperature" else not form.show_thinking)
            result = dialog.save(form)
        else:
            # A settled seat: what it holds in memory is what it last saved.
            object.__setattr__(a.settings, "_baseline", asdict(a.settings))
            setattr(a.settings, field, 0.5 if field == "temperature" else not a.settings.show_thinking)
            result = settings_runtime.persist_or_raise(a, a.settings)
        assert [outcome.fields for outcome in result.persistence if outcome.saved] == [(field,)]
        assert _on_disk(path)[:2] == (AUTONOMOUS, PLANTED_ALLOW), "the plant survived the save"
        assert a.chosen_tool_profile == (AUTONOMOUS if adopted else STRICT)
        assert seat_authority.turn_profile(a, "typed", a.chosen_tool_profile) == a.chosen_tool_profile
        assert a._active_tool_profile == STRICT, "the turn in flight is not changed"
        assert a.settings.tool_always_allow == [], "the allow list in force is not re-read by a save"
    finally:
        a.store.release()


def test_measured_an_empty_folder_below_agents_stops_every_seat_and_the_rule_says_nothing(
        app, tmp_path, monkeypatch):
    """NOT the rule's to stop, pinned so the limit is not read wider than it is.
    Catalog discovery, launch, adoption or listing still rejects an empty
    sibling folder. Owned actions check only their own home, where an
    initializing marker still blocks authority. Neither operation names a
    settings file, so this rule has nothing to judge."""
    a, _ = _agent(app, monkeypatch)
    session = a._agent_session
    agents = tmp_path / ".agents"
    marker = agents / "Probe" / agent_store.INITIALIZING_NAME
    for args in ({"command": f"mkdir {(agents / 'x').as_posix()}"},
                 {"path": str(marker), "content": "x"}, {"command": f"echo x > {marker.as_posix()}"}):
        assert seat_authority.settings_file_refusal(a, args, tmp_path) is None, args
    assert session.authority.model == "fixture", "CONTROL: the seat works"
    (agents / "note.txt").write_text("x", encoding="utf-8")
    assert session.authority.model == "fixture", "CONTROL: a plain file below `.agents` is not read"
    (agents / "x").mkdir()
    # Owned authority skips sibling damage; next catalog discovery, launch,
    # adoption or listing still checks it.
    assert session.authority.model == "fixture"
    # Owned saves also skip sibling damage; next catalog discovery, launch,
    # adoption or listing still checks it.
    assert convo_settings.save(a.convo_dir, convo_settings.ConvoSettings(),
                               agent_session=session) == a.convo_dir / SETTINGS
    with pytest.raises(agent_store.StoreError, match="absent or unreadable"):
        agent_ownership.AgentSession.acquire_existing(session.store, name="Probe")
    (agents / "x").rmdir()
    assert session.authority.model == "fixture", "CONTROL: the folder removed, the seat works again"
    marker.write_text("x", encoding="utf-8")
    with pytest.raises(agent_store.StoreError):
        session.authority
    marker.unlink()
    assert session.authority.model == "fixture"


def test_measured_a_deleted_conversation_file_also_drops_the_born_in_a_spawned_seat_fact(
        app, tmp_path, monkeypatch):
    """The conversation's file records that it was born in a spawned seat, and a
    resume WITHOUT the spawn marker reads that record to keep it one. With the
    record changed, or the file deleted (a delete this rule does not catch when
    it does not name the file), the resumed seat is no longer counted as spawned."""
    a, _ = _agent(app, monkeypatch, STRICT)
    monkeypatch.setattr(m.LiteTUI, "_render_resumed", lambda self, path: None)
    monkeypatch.setattr(m.LiteTUI, "connect", lambda self, *_a, **_k: None)
    try:
        a._spawned_marker = True                       # born in a spawned seat
        a._materialise_convo()
        a._append({"role": "user", "content": "saved"})
        path = a.convo_dir / SETTINGS
        assert json.loads(path.read_text(encoding="utf-8"))["seat_spawned"] is True
        a._spawned_marker = False                      # relaunched without the marker
        assert a._resume(a.convo_path) and a._spawned_seat is True, "the record keeps it a spawned seat"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["seat_spawned"] = False
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert a._resume(a.convo_path) and a._spawned_seat is False, "the record changed"
        a._spawned_seat = True
        path.unlink()
        assert a._resume(a.convo_path) and a._spawned_seat is False, "the file deleted"
    finally:
        a.store.release()


def test_measured_a_reconnect_adopts_the_planted_hosts_and_skill_roots(app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch, STRICT)
    try:
        path = _born(a)
        _plant(path, lm_host=PLANTED_HOST, skill_roots=["/planted/skills"], tool_always_allow=PLANTED_ALLOW)
        assert a.settings.lm_host != PLANTED_HOST
        settings_runtime.prepare_reconnect(a)
        assert a.settings.lm_host == PLANTED_HOST and a.settings.skill_roots == ["/planted/skills"]
        assert a.settings.tool_always_allow == [] and a.chosen_tool_profile == STRICT, (
            "a reconnect takes the reconnect-time keys only")
    finally:
        a.store.release()


def test_measured_an_agents_home_file_decides_its_next_launch_and_stops_a_running_seat(
        app, tmp_path, monkeypatch):
    a, _ = _agent(app, monkeypatch)
    session = a._agent_session
    store = agent_store.AgentStore(tmp_path)
    other_id = "22222222-2222-4222-8222-222222222222"
    other = tmp_path / ".agents" / "Other"
    other.mkdir()
    (other / SETTINGS).write_text(json.dumps({
        "schema_version": 1, "name": "Other", "agent_id": other_id,
        "execution": {"backend": "lmstudio", "model": "planted-model", "thinking_level": "high"},
    }), encoding="utf-8")
    planted = (other / SETTINGS).read_text(encoding="utf-8")
    # An agent that is not running: its next launch reads the file, nothing else.
    with agent_ownership.AgentSession.acquire_existing(store, agent_id=other_id) as launched:
        assert (launched.authority.backend, launched.authority.model) == ("lmstudio", "planted-model")
    assert session.authority.model == "fixture", "CONTROL: this seat is untouched so far"
    # Garbled sibling metadata still blocks catalog-validated acquisition.
    (other / SETTINGS).write_text("{not json", encoding="utf-8")
    with pytest.raises(agent_store.StoreError, match="absent or unreadable"):
        agent_ownership.AgentSession.acquire_existing(store, agent_id=other_id)
    # Owned authority skips sibling damage; next catalog discovery, launch,
    # adoption or listing still checks it.
    assert session.authority.model == "fixture"
    # Owned saves also skip sibling damage; next catalog discovery, launch,
    # adoption or listing still checks it.
    assert convo_settings.save(a.convo_dir, convo_settings.ConvoSettings(),
                               agent_session=session) == a.convo_dir / SETTINGS
    (other / SETTINGS).write_text(planted, encoding="utf-8")
    assert session.authority.model == "fixture", "CONTROL: repaired, this seat works again"
    # A RUNNING seat's own home file, changed behind it: the seat does not take
    # the new backend; every use of its ownership fails until it is reopened.
    home = tmp_path / ".agents" / "Probe" / SETTINGS
    raw = json.loads(home.read_text(encoding="utf-8"))
    raw["execution"].update(backend="lmstudio", model="planted-model")
    home.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(agent_store.StoreError, match="authority changed"):
        session.authority
    with pytest.raises(agent_store.StoreError, match="authority changed"):
        convo_settings.save(a.convo_dir, convo_settings.ConvoSettings(), agent_session=session)


def test_measured_the_legacy_copy_is_never_loaded(app, tmp_path, monkeypatch):
    """<root>/.convos/<id>/settings.json: a seat refuses to resume a transcript
    outside its own agent, at a /resume and at startup, and its settings service
    reads its own agent's conversations folder only. So the legacy copy is not in
    the rule. (The legacy file says strict and carries an allow row; the device
    default is another profile, so a read of it would show.)"""
    a, _ = _agent(app, monkeypatch, INTERACTIVE)
    said: list = []
    a._system = lambda text, *_a, **_k: said.append(text)
    legacy = tmp_path / ".convos" / "33333333-3333-4333-8333-333333333333"
    legacy.mkdir(parents=True)
    (legacy / "convo.jsonl").write_text("".join(json.dumps(row) + "\n" for row in (
        {"type": "meta", "id": legacy.name},
        {"type": "msg", "message": {"role": "user", "content": "old"}})), encoding="utf-8")
    (legacy / SETTINGS).write_text(json.dumps({
        "tool_policy_profile": STRICT, "execution": {"tool_always_allow": PLANTED_ALLOW}}), encoding="utf-8")
    mine = a.convo_dir
    assert a._resume(legacy / "convo.jsonl") is False
    a._cli_convo_id = legacy.name
    assert a._resume_cli_conversation() is False
    assert any("outside the selected agent" in text for text in said), said
    assert a.convo_dir == mine and a.chosen_tool_profile == INTERACTIVE
    assert a.settings.tool_always_allow == []
    service = settings_runtime.service_for(a)
    assert service.conversation_root == mine.parent
    seen = service.snapshot(legacy.name).effective
    assert seen.tool_always_allow == [] and seen.tool_policy_profile != STRICT


# ── the mirror: every schedule-file test run again; what it asks its rule is
#    asked of this rule too (107 of the 111 ask something, see ASKS_NOTHING) ──

MIRRORED: list = []
#: Schedule-file tests that never ask their rule a question, so their copies here
#: pass whatever the settings rule does. Pinned by the mirror: a copy that asks
#: nothing and is not named here fails, and so does a named one that starts asking.
ASKS_NOTHING = frozenset({
    "ryans_manual_autonomy_choice_is_not_capped",       # drives a key press, no tool call
    "ryans_scheduler_save_and_edit_remain_untouched",   # scheduler.save, in process
    "scheduler_save_is_untouched_in_an_agent_seat",     # scheduler.save, in process
    "the_litetui_floor_leaves_jobs_to_the_seat",        # asks tool_policy's floor, not the rule
})


def _renamed(value, to=SETTINGS):
    """`value` with the schedule file's name replaced by the settings file's."""
    if isinstance(value, str):
        return re.sub(r"(?i)jobs\.json", to, value)
    if isinstance(value, dict):
        return {key: _renamed(item, to) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_renamed(item, to) for item in value)
    return value


#: The same question moved below `.agents`: wherever a test names the schedule
#: file, an agent's home settings file in the same data root.
BELOW = ".agents/Seat/" + SETTINGS


@pytest.fixture
def _mirror(request, monkeypatch):
    """Whatever a test asks the schedule-file rule, ask the settings rule the same
    thing with the name substituted, at the same moment, and require the same
    verdict. The original answer is returned, so the test itself is unchanged."""
    ask = seat_authority.jobs_file_refusal
    before = len(MIRRORED)

    def both(app, args, workspace, policy=None, *, shell=None):
        why = ask(app, args, workspace, policy, shell=shell)
        assert JOBS not in str(workspace).lower(), "CONTROL: the rename must not touch a folder"
        twin = seat_authority.settings_file_refusal(app, _renamed(args), workspace, policy, shell=shell)
        MIRRORED.append(bool(why))
        assert bool(twin) == bool(why), (args, "schedule file:", why, "settings file:", twin)
        if twin:
            assert "T0306" in twin and JOBS not in twin, twin
        below = seat_authority.settings_file_refusal(app, _renamed(args, BELOW), workspace, policy,
                                                     shell=shell)
        assert bool(below) == bool(why), (args, "schedule file:", why, "below .agents:", below)
        if below:
            assert "T0306" in below and JOBS not in below, below
        return why

    monkeypatch.setattr(seat_authority, "jobs_file_refusal", both)
    yield
    # A copy builds its app through the schedule-file module's helper, whose own
    # release fixture runs only for tests of that module: give its sessions back
    # here. (Absent once that module takes its seat from a shared fixture.)
    held = getattr(jobs_tests, "_SESSIONS", [])
    while held:
        held.pop().release()
    name = request.node.originalname.removeprefix("test_mirror_of_")
    asked_nothing = len(MIRRORED) == before
    assert asked_nothing == (name in ASKS_NOTHING), (
        name, "asked its rule nothing" if asked_nothing else "now asks its rule: unpin it")


def _mirrored(test):
    """A copy of `test` that runs under the mirror. A copy: marking the original
    would make the schedule-file module ask for a fixture it does not have."""
    copy = types.FunctionType(test.__code__, test.__globals__, test.__name__,
                              test.__defaults__, test.__closure__)
    copy.__kwdefaults__ = test.__kwdefaults__
    copy.__dict__.update({key: (list(item) if key == "pytestmark" else item)
                          for key, item in test.__dict__.items()})
    return pytest.mark.usefixtures("_mirror")(copy)


for _name, _test in list(vars(jobs_tests).items()):
    if _name.startswith("test_") and callable(_test):
        globals()["test_mirror_of_" + _name[len("test_"):]] = _mirrored(_test)


def test_the_mirror_can_fail(app, tmp_path, monkeypatch, _mirror):
    """CONTROL: with the settings rule silenced, a mirrored refusal must not pass."""
    a, _ = _agent(app, monkeypatch)
    monkeypatch.setattr(seat_authority, "settings_file_refusal", lambda *_a, **_k: None)
    with pytest.raises(AssertionError, match="settings file"):
        seat_authority.jobs_file_refusal(a, {"path": str(tmp_path / JOBS)}, tmp_path)


def test_the_mirror_can_fail_below_agents(app, tmp_path, monkeypatch, _mirror):
    """CONTROL: with the settings rule asking about the device file only (as it
    did before the files below `.agents` were added), the second mirrored
    question must not pass."""
    a, _ = _agent(app, monkeypatch)
    monkeypatch.setattr(
        seat_authority, "settings_file_refusal",
        lambda app, args, workspace, policy=None, **kw: seat_authority._owned_file_refusal(
            app, args, workspace, policy, ((SETTINGS, seat_authority._settings_words, (None,), False),), **kw))
    with pytest.raises(AssertionError, match="below .agents"):
        seat_authority.jobs_file_refusal(a, {"path": str(tmp_path / JOBS)}, tmp_path)


def test_zz_the_mirror_asked_refusals_and_passes():
    """Last in the file: the mirrored tests above asked both kinds of question."""
    if not MIRRORED:
        pytest.skip("no mirrored test ran in this session")
    print(f"\nMIRROR questions={len(MIRRORED)} refused={sum(MIRRORED)} allowed={len(MIRRORED) - sum(MIRRORED)}")
    assert any(MIRRORED) and not all(MIRRORED)
