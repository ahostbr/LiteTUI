"""T0306 — the device settings file is Owner's: only Owner's own seat may write it.

A data root's settings.json holds the standing allow and deny rules, the tool
profile and the paths of the programs LiteTUI launches, and every seat on that
data root shares the one file. These arms hold the ownership refusal at the
shared _authorize_action door, beside the jobs.json one (T1085).

The bound, for a write or edit tool's path: a file whose name Windows opens as
settings.json (deny_floor.canonical_name, so a stream suffix and a trailing dot
or space are the same file) in a folder that holds `src/litetui`, or
`.litetui-data.json`, or is $LITETUI_DATA_ROOT (the floor's own folder test).

NOT covered here: the bash and PowerShell forms; a link or an 8.3 short name to
the file; a path held in a variable; a write from inside a script file; a
per-agent home's or a conversation's own settings.json (a different file, by
the folder test); writers that are not LiteTUI seats. The rule brings
settings.json level with jobs.json. It does not make a seat a sandbox.
"""
from __future__ import annotations

import json

import pytest

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


def test_the_settings_screens_own_save_is_untouched(app, tmp_path, monkeypatch):
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
