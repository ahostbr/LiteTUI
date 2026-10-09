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
settings rule, the name substituted. At this commit 107 of those 111 tests ask
their rule something and so are mirrored; 4 ask it nothing and pass whatever the
settings rule does (named at ASKS_NOTHING, and pinned). A case added to that
file later is mirrored without an edit here.

NOT covered, by this rule or by the schedule file's (they share every one):
- everything in the ceiling above;
- a wildcard or a brace in the name (`settings.jso?`, `*.json`, `{settings,x}.json`);
- a whole-folder copy, extract or clean that never names the file;
- the data root's marker removed first, for a root known only by its marker;
- a PowerShell expression that carries the path in a .NET call;
- a path under an argument key the rule does not read (it reads `path`,
  `file_path`, `notebook_path`, a `command`, and a patch's file headers);
- a seat editing the installed source; the Claude Code hook path, which does not
  judge this file at all (only a LiteTUI seat does).
A delete or a garbled file is not a lesser case of those: the file then loads as
its defaults, which are the autonomous profile with no deny rules.
Each conversation's own settings.json, which holds the allow and deny rules and
the profile that conversation actually runs under, and a per-agent home's, are
different files by the folder test and are NOT covered. The rule brings the
device settings.json level with jobs.json. It does not make a seat a sandbox.
"""
from __future__ import annotations

import json
import re
import types

import pytest

import test_jobs_file_t1085 as jobs_tests

from litetui import agent_ownership, agent_store, claude_tools, seat_authority, tool_policy
from litetui import app as m
from litetui import settings as settings_mod
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
    for folder in ("elsewhere", ".vscode", ".agents/Name"):
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
                                    ".vscode/settings.json", ".agents/Probe/settings.json"])
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


def _renamed(value):
    """`value` with the schedule file's name replaced by the settings file's."""
    if isinstance(value, str):
        return re.sub(r"(?i)jobs\.json", SETTINGS, value)
    if isinstance(value, dict):
        return {key: _renamed(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_renamed(item) for item in value)
    return value


@pytest.fixture
def _mirror(request, monkeypatch):
    """Whatever a test asks the schedule-file rule, ask the settings rule the same
    thing with the name substituted, at the same moment, and require the same
    verdict. The original answer is returned, so the test itself is unchanged."""
    ask = seat_authority.jobs_file_refusal
    before = len(MIRRORED)

    def both(app, args, workspace, policy=None):
        why = ask(app, args, workspace, policy)
        assert JOBS not in str(workspace).lower(), "CONTROL: the rename must not touch a folder"
        twin = seat_authority.settings_file_refusal(app, _renamed(args), workspace, policy)
        MIRRORED.append(bool(why))
        assert bool(twin) == bool(why), (args, "schedule file:", why, "settings file:", twin)
        if twin:
            assert "T0306" in twin and JOBS not in twin, twin
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


def test_zz_the_mirror_asked_refusals_and_passes():
    """Last in the file: the mirrored tests above asked both kinds of question."""
    if not MIRRORED:
        pytest.skip("no mirrored test ran in this session")
    print(f"\nMIRROR questions={len(MIRRORED)} refused={sum(MIRRORED)} allowed={len(MIRRORED) - sum(MIRRORED)}")
    assert any(MIRRORED) and not all(MIRRORED)
