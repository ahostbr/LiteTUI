"""Owner mode: `--owner NAME --memory DIR` flags, validation and store injection."""
from __future__ import annotations

import os
import shutil
import sys

import pytest

from litetui import app as app_mod
from litetui import appsvc, cli, owner_mode, settings
from litetui.agent_launch_context import ordinary
from litetui.plugins import convo
from litetui.settings import Settings
from litetui.textfmt import memory_prompt, refresh_store_address

SOUL, NOW, DOMAIN = "CHARTER-LINE-ALPHA", "NOW-LINE-ALPHA", "DOMAIN-LINE-ALPHA"
HOME_SOUL = "HOME-SOUL-SHOULD-NOT-APPEAR"


@pytest.fixture
def owner_dir(tmp_path):
    folder = tmp_path / "widgets-memory"
    folder.mkdir()
    (folder / "SOUL.md").write_text(SOUL + "\n", encoding="utf-8")
    (folder / "NOW.md").write_text(NOW + "\n", encoding="utf-8")
    (folder / "DOMAIN.md").write_text(DOMAIN + "\n", encoding="utf-8")
    return folder


# -- validation ---------------------------------------------------------------

def test_a_complete_absolute_folder_is_accepted(owner_dir):
    assert owner_mode.validate_memory_dir(str(owner_dir)) == owner_dir


@pytest.mark.parametrize("missing", ["SOUL.md", "NOW.md", "DOMAIN.md"])
def test_each_missing_file_is_a_refusal_that_names_it(owner_dir, missing):
    (owner_dir / missing).unlink()
    with pytest.raises(owner_mode.OwnerModeError, match=missing):
        owner_mode.validate_memory_dir(str(owner_dir))


def test_relative_absent_and_file_paths_are_refused(tmp_path, owner_dir):
    for bad in ("relative/dir", str(tmp_path / "absent"), str(owner_dir / "NOW.md"), ""):
        with pytest.raises(owner_mode.OwnerModeError):
            owner_mode.validate_memory_dir(bad)


def test_a_linked_folder_or_file_is_refused(tmp_path, owner_dir):
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(owner_dir, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create symlinks")
    with pytest.raises(owner_mode.OwnerModeError, match="sit under"):
        owner_mode.validate_memory_dir(str(alias))
    with pytest.raises(owner_mode.OwnerModeError, match="sit under"):
        owner_mode.validate_memory_dir(str(alias / "sub"))
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    (owner_dir / "DOMAIN.md").unlink()
    (owner_dir / "DOMAIN.md").symlink_to(outside)
    with pytest.raises(owner_mode.OwnerModeError, match="link"):
        owner_mode.validate_memory_dir(str(owner_dir))


# -- the owner-handoff availability probe ---------------------------------------

def _fake_cli(monkeypatch, code):
    monkeypatch.setattr(owner_mode, "handoff_command", lambda: [sys.executable, "-c", code, "--"])


REFUSING = "import sys; print('HANDOFF REFUSED: not a directory. Nothing was committed.'); sys.exit(2)"


def test_the_probe_accepts_a_command_that_refuses_in_its_own_words(monkeypatch):
    _fake_cli(monkeypatch, REFUSING)
    owner_mode.require_handoff()


@pytest.mark.parametrize("code", [
    "import sys; sys.exit(0)",
    "import sys; print('Unknown command: owner-handoff'); sys.exit(1)",
    "import sys; print('HANDOFF REFUSED: x'); sys.exit(0)",
    "raise SystemExit('boom')",
])
def test_the_probe_refuses_anything_that_is_not_the_real_command(monkeypatch, code):
    _fake_cli(monkeypatch, code)
    with pytest.raises(owner_mode.OwnerModeError, match="owner-handoff"):
        owner_mode.require_handoff()


def test_the_probe_refuses_a_missing_executable(monkeypatch):
    monkeypatch.setattr(owner_mode, "handoff_command", lambda: ["definitely-not-an-installed-binary"])
    with pytest.raises(owner_mode.OwnerModeError, match="owner-handoff"):
        owner_mode.require_handoff()


@pytest.mark.skipif(shutil.which("liteharness") is None, reason="liteharness is not installed here")
def test_the_installed_liteharness_passes_the_real_probe():
    owner_mode.require_handoff()


def test_run_handoff_reports_the_reason_on_refusal(monkeypatch, owner_dir):
    _fake_cli(monkeypatch, REFUSING)
    ok, reason = owner_mode.run_handoff(owner_dir, agent_id="a-1")
    assert not ok and "HANDOFF REFUSED" in reason


# -- the CLI: every refusal happens before anything is created ----------------------

class _FakeApp:
    built: list = []

    def __init__(self, **kwargs):
        type(self).built.append(kwargs)

    def run(self, **_kwargs):
        return None


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    _FakeApp.built = []
    monkeypatch.setattr("litetui.app.LiteTUI", _FakeApp)
    monkeypatch.setattr("litetui.shared_state.check_data_version", lambda _root: None)
    monkeypatch.setattr(owner_mode, "require_handoff", lambda: None)

    def run(*argv):
        monkeypatch.setattr("sys.argv", ["litetui", *argv])
        cli.main()
    return run


def _nothing_created(tmp_path):
    return not (tmp_path / ".agents").exists()


@pytest.mark.parametrize("argv", [
    lambda d: ["--owner", "Alpha"],
    lambda d: ["--memory", str(d)],
    lambda d: ["--owner", "Alpha", "--memory", "relative/dir"],
    lambda d: ["--owner", "Alpha", "--memory", str(d / "absent")],
    lambda d: ["--owner", "Alpha", "--memory", str(d), "--agent", "Alpha"],
    lambda d: ["--owner", "Alpha", "--memory", str(d), "--create-agent", "Alpha"],
])
def test_every_refusal_leaves_no_agent_home(tmp_path, owner_dir, cli_env, argv):
    with pytest.raises(SystemExit) as exc:
        cli_env(*argv(owner_dir))
    assert exc.value.code == 2
    assert _nothing_created(tmp_path) and not _FakeApp.built


def test_a_missing_file_refuses_before_anything_is_created(tmp_path, owner_dir, cli_env):
    (owner_dir / "NOW.md").unlink()
    with pytest.raises(SystemExit):
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert _nothing_created(tmp_path) and not _FakeApp.built


def test_a_missing_owner_handoff_refuses_before_anything_is_created(tmp_path, owner_dir, cli_env, monkeypatch):
    monkeypatch.setattr(owner_mode, "require_handoff",
                        lambda: (_ for _ in ()).throw(owner_mode.OwnerModeError("owner mode needs owner-handoff")))
    with pytest.raises(SystemExit):
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert _nothing_created(tmp_path) and not _FakeApp.built


def test_a_good_launch_opens_the_named_home_and_hands_the_folder_to_the_app(tmp_path, owner_dir, cli_env):
    cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert len(_FakeApp.built) == 1
    assert _FakeApp.built[0]["owner_memory_dir"] == owner_dir
    assert _FakeApp.built[0]["agent_session"] is not None
    assert (tmp_path / ".agents" / "Alpha").is_dir()


def test_the_flags_are_per_launch_and_never_saved(tmp_path, owner_dir, cli_env):
    cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    saved = (tmp_path / ".agents" / "Alpha" / "settings.json").read_text(encoding="utf-8")
    assert "memory" not in saved.lower() and str(owner_dir) not in saved and "owner" not in saved.lower()
    cli_env()
    assert _FakeApp.built[-1]["owner_memory_dir"] is None


def test_a_busy_home_is_refused_rather_than_opening_another_name(tmp_path, owner_dir, cli_env, capsys):
    import dataclasses
    # Alpha is held by another launch, so ordinary() would fall back to a generated name.
    held = ordinary(tmp_path, dataclasses.replace(settings.load(), seat_name="Alpha"))
    try:
        assert held.authority.name == "Alpha"
        with pytest.raises(SystemExit) as exc:
            cli_env("--owner", "Alpha", "--memory", str(owner_dir))
        assert exc.value.code == 2
        assert not _FakeApp.built
        err = capsys.readouterr().err
        assert "--owner Alpha" in err and "in use by another running LiteTUI" in err
        assert "choose a different --owner name" in err, "the refusal says how to resolve it"
        assert sorted(p.name for p in (tmp_path / ".agents").iterdir() if p.is_dir()) == ["Alpha"], \
            "no fallback home was created"
    finally:
        held.release()


def test_a_spawned_seat_name_that_differs_is_refused_with_both_names(tmp_path, owner_dir, cli_env, capsys, monkeypatch):
    from litetui import harness
    monkeypatch.setenv(harness.SPAWN_IDENTITY_MARKER, "1")
    monkeypatch.setattr(harness, "spawned_seat_identity",
                        lambda *a, **k: ("22222222-2222-4222-8222-222222222222", "Beta", "worker"))
    with pytest.raises(SystemExit) as exc:
        cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert exc.value.code == 2 and not _FakeApp.built
    err = capsys.readouterr().err
    assert "--owner Alpha" in err and "Beta" in err
    assert "relaunch with --owner Beta" in err and "spawn the seat as Alpha" in err
    assert not (tmp_path / ".agents").exists()


# -- injection --------------------------------------------------------------------------

@pytest.fixture
def make_app(tmp_path, owner_dir):
    made = []

    def build(*, owner: bool = True, **setting_kwargs) -> app_mod.LiteTUI:
        session = ordinary(tmp_path, settings.load())
        app = app_mod.LiteTUI(agent_session=session, owner_memory_dir=owner_dir if owner else None)
        made.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._materialise_convo()
        for name in ("memory.md", "handoff.md"):
            (session.memory_root / name).write_text("", encoding="utf-8")
        (session.memory_root / "soul.md").write_text(HOME_SOUL + "\n", encoding="utf-8")
        if setting_kwargs:
            app.settings = Settings(**setting_kwargs)
        return app

    yield build
    for app in reversed(made):
        try:
            app.store.release()
        finally:
            app._agent_session.release()


def test_the_three_owner_files_are_injected_in_order_instead_of_the_home(make_app):
    app = make_app()
    text = app._request_messages()[0]["content"]
    assert text.count(appsvc.STORE_HEADER) == 1
    assert text.index(SOUL) < text.index(NOW) < text.index(DOMAIN)
    assert HOME_SOUL not in text
    for heading in ("### SOUL.md", "### NOW.md", "### DOMAIN.md"):
        assert heading in text
    assert "### soul.md" not in text and "### memory.md" not in text


def test_without_owner_mode_the_agent_home_files_are_injected_as_before(make_app):
    app = make_app(owner=False)
    text = app._request_messages()[0]["content"]
    assert HOME_SOUL in text and "### soul.md" in text
    assert SOUL not in text and "NOW.md" not in text


def test_caps_cut_each_file_with_the_truncation_note(make_app, owner_dir):
    (owner_dir / "SOUL.md").write_text("S" * 9000, encoding="utf-8")
    (owner_dir / "NOW.md").write_text("N" * 7000, encoding="utf-8")
    (owner_dir / "DOMAIN.md").write_text("D" * 31000, encoding="utf-8")
    app = make_app()
    block = appsvc.store_block(app)
    assert "S" * 8000 in block and "S" * 8001 not in block
    assert "N" * 6000 in block and "N" * 6001 not in block
    assert "D" * 30000 in block and "D" * 30001 not in block
    assert block.count("truncated at") == 3
    assert str(owner_dir / "DOMAIN.md") in block, "the note names the file to read for the rest"


def test_the_domain_cap_is_a_setting(make_app, owner_dir):
    (owner_dir / "DOMAIN.md").write_text("D" * 500, encoding="utf-8")
    app = make_app(owner_domain_cap=100)
    block = appsvc.store_block(app)
    assert "D" * 100 in block and "D" * 101 not in block


def test_the_default_domain_cap_is_thirty_thousand():
    assert Settings().owner_domain_cap == 30000
    assert owner_mode.store_files(Settings(owner_domain_cap=0))[2] == ("DOMAIN.md", 30000)


def test_the_prompt_names_the_folder_and_not_the_agent_home(make_app, owner_dir):
    app = make_app()
    prompt = app._system_prompt_text()
    assert str(owner_dir).replace("\\", "/") in prompt
    assert "NOW.md" in prompt and "Do NOT redo" in prompt and "Next 3 steps" in prompt
    assert "SNAPSHOT" in prompt or "snapshot" in prompt
    home = str(app._agent_session.memory_root).replace("\\", "/")
    assert f"{home}/memory.md" not in prompt


def test_the_claude_backend_prompt_carries_the_owner_files(make_app):
    from litetui.claude_turn import ledger_for, system_prompt_for
    app = make_app()
    segment = ledger_for(app).select_segment("ws")
    prompt = system_prompt_for(app, segment)
    assert prompt.index(SOUL) < prompt.index(NOW) < prompt.index(DOMAIN)
    assert HOME_SOUL not in prompt


# -- the address stanza is not rewritten back to the agent home --------------------

def test_refresh_keeps_the_owner_stanza_and_is_idempotent(owner_dir, tmp_path):
    base = "BASE PROMPT\n" + memory_prompt("cid-1", owner_dir, owner=True) + "\nuser prose after\n"
    again = refresh_store_address(base, "cid-1", owner_dir, owner=True)
    assert again == base
    moved = refresh_store_address(base, "cid-2", owner_dir, owner=True)
    assert moved.count("## Your conversation store") == 1
    assert "cid-2" in moved and "cid-1" not in moved and "user prose after" in moved
    assert str(owner_dir).replace("\\", "/") in moved


def test_refresh_replaces_a_home_stanza_when_resumed_in_owner_mode(owner_dir, tmp_path):
    home = tmp_path / "home"
    old = "BASE\n" + memory_prompt("cid-1", home) + "\ntail\n"
    new = refresh_store_address(old, "cid-1", owner_dir, owner=True)
    assert new.count("## Your conversation store") == 1, "replaced, not appended"
    assert str(owner_dir).replace("\\", "/") in new and "memory.md" not in new
    assert new.endswith("\ntail\n")


def test_refresh_without_owner_mode_is_unchanged_for_a_home_stanza(tmp_path):
    home = tmp_path / "home"
    old = "BASE\n" + memory_prompt("cid-1", home) + "\ntail\n"
    assert refresh_store_address(old, "cid-1", home) == old


# -- banners ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_new_banner_lists_the_owner_files(make_app, owner_dir):
    app = make_app()
    seen = []
    async with app.run_test(size=(120, 40)):
        app.system_message = seen.append
        convo._cmd_new(app, "new", "")
    banner = "\n".join(seen)
    assert "SOUL.md" in banner and "NOW.md" in banner and "DOMAIN.md" in banner
    assert "handoff.md" not in banner


@pytest.mark.asyncio
async def test_the_new_banner_is_unchanged_without_owner_mode(make_app):
    app = make_app(owner=False)
    seen = []
    async with app.run_test(size=(120, 40)):
        app.system_message = seen.append
        convo._cmd_new(app, "new", "")
    assert "memory.md · soul.md · handoff.md" in "\n".join(seen)


def _help_text(app, monkeypatch) -> str:
    from litetui.plugins import help_plugin
    shown = []
    monkeypatch.setattr(help_plugin, "present_dialog", lambda _app, body, _screen: shown.append(body.args[0]))
    help_plugin._cmd_help(app, "/help", "")
    return shown[0]


def test_help_names_the_owner_files_in_owner_mode(make_app, owner_dir, monkeypatch):
    text = _help_text(make_app(), monkeypatch)
    assert f"store: {owner_dir} holds SOUL.md, NOW.md, DOMAIN.md" in text
    assert "handoff.md" not in text.split("store:")[1].splitlines()[0]


def test_help_is_unchanged_without_owner_mode(make_app, monkeypatch):
    text = _help_text(make_app(owner=False), monkeypatch)
    assert "store: .agents/<Name>/ holds memory.md, soul.md, handoff.md and memories/" in text


# -- resume in owner mode: the address stanza follows the owner folder ---------------------

@pytest.mark.asyncio
async def test_same_owner_resume_keeps_the_stanza_at_the_owner_folder(make_app, owner_dir):
    import json
    app = make_app()
    home = app._agent_session.memory_root
    saved = refresh_store_address(app._system_prompt_text(), "old-id", owner_dir, owner=True) + "\nuser prose\n"
    path = app.convo_dir / "convo.jsonl"
    path.write_text(json.dumps({"type": "meta", "v": 3, "id": app.convo_dir.name}) + "\n"
                    + json.dumps({"type": "snapshot", "messages": [{"role": "system", "content": saved}]}) + "\n",
                    encoding="utf-8")
    app._render_resumed = lambda _path: None
    async with app.run_test(size=(120, 40)):
        assert app._resume(path, startup=True)
        body = app.conversation[0]["content"]
    assert body.count("## Your conversation store") == 1
    assert "owner memory folder" in body and "Do NOT redo" in body, "the owner template, not the home one"
    assert str(owner_dir).replace("\\", "/") in body
    assert f"{str(home).replace(chr(92), '/')}/memory.md" not in body
    assert body.endswith("\nuser prose\n")


# -- launch refusals that the first pass left to an incidental failure -------------------------

def test_the_pair_refusal_says_so(tmp_path, owner_dir, cli_env, capsys):
    with pytest.raises(SystemExit):
        cli_env("--owner", "Alpha")
    assert "together" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli_env("--memory", str(owner_dir))
    assert "together" in capsys.readouterr().err


def test_owner_cannot_be_combined_with_an_existing_agent(tmp_path, owner_dir, cli_env, capsys):
    ordinary(tmp_path, __import__("dataclasses").replace(settings.load(), seat_name="Alpha")).release()
    with pytest.raises(SystemExit) as exc:
        cli_env("--owner", "Alpha", "--memory", str(owner_dir), "--agent", "Alpha")
    assert exc.value.code == 2 and not _FakeApp.built
    assert "--owner" in capsys.readouterr().err


def test_a_relative_memory_path_is_refused_even_when_it_exists_here(tmp_path, owner_dir, monkeypatch):
    monkeypatch.chdir(owner_dir.parent)
    with pytest.raises(owner_mode.OwnerModeError, match="absolute"):
        owner_mode.validate_memory_dir(owner_dir.name)


def test_the_launch_does_not_mutate_the_loaded_settings(tmp_path, owner_dir, cli_env, monkeypatch):
    loaded = settings.load()
    monkeypatch.setattr("litetui.settings.load", lambda: loaded)
    before = loaded.seat_name
    cli_env("--owner", "Alpha", "--memory", str(owner_dir))
    assert loaded.seat_name == before and before != "Alpha"


# -- re-injection and segment pins, in owner mode (the store re-injection contract, for the owner files) -----

NEW_NOW = "NOW-LINE-BETA-2002"


def _now(owner_dir, text):
    (owner_dir / "NOW.md").write_text(text + "\n", encoding="utf-8")


def _records(app) -> int:
    return len((app.convo_dir / "convo.jsonl").read_text(encoding="utf-8").splitlines())


@pytest.mark.asyncio
async def test_owner_files_come_back_once_after_ctrl_t_and_plan_mode(make_app, owner_dir):
    app = make_app()
    async with app.run_test(size=(120, 40)):
        assert app._request_messages()[0]["content"].count(appsvc.STORE_HEADER) == 1
        _now(owner_dir, NEW_NOW)
        app.action_toggle_tools()
        assert appsvc.STORE_HEADER not in app.conversation[0]["content"]
        text = app._request_messages()[0]["content"]
        assert text.count(appsvc.STORE_HEADER) == 1 and NEW_NOW in text and NOW not in text
        _now(owner_dir, NEW_NOW + "-PLAN")
        assert app.set_plan_mode(True, announce=False)
        text = app._request_messages()[0]["content"]
        assert text.count(appsvc.STORE_HEADER) == 1 and NEW_NOW + "-PLAN" in text
        assert SOUL in text and DOMAIN in text and HOME_SOUL not in text


@pytest.mark.asyncio
async def test_owner_new_conversation_gets_a_fresh_owner_snapshot(make_app, owner_dir):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        assert app._request_messages()[0]["content"].count(appsvc.STORE_HEADER) == 1
        _now(owner_dir, NEW_NOW)
        convo._cmd_new(app, "new", "")
        await pilot.pause()
        text = app._request_messages()[0]["content"]
        assert text.count(appsvc.STORE_HEADER) == 1 and NEW_NOW in text and NOW not in text


def test_owner_resumed_snapshot_is_not_reinjected(make_app, owner_dir):
    first = make_app()
    saved = first._request_messages()[0]["content"]
    _now(owner_dir, NEW_NOW)
    resumed = make_app()
    resumed.conversation[0] = {"role": "system", "content": saved}
    from litetui.conversation import ConversationRepository
    resumed._store_injected = ConversationRepository.read(first.convo_path)[0]["store_injected"]
    before = _records(resumed)
    out = resumed._request_messages()[0]["content"]
    assert out == saved and out.count(appsvc.STORE_HEADER) == 1 and NOW in out and NEW_NOW not in out
    assert _records(resumed) == before, "a resumed snapshot writes no edit record"


def test_owner_injection_persists_one_edit_per_snapshot_not_per_turn(make_app):
    app = make_app()
    before = _records(app)
    for _ in range(5):
        app._request_messages()
    assert _records(app) == before + 1


def test_the_claude_prompt_is_fixed_for_a_segment_and_a_new_segment_sees_the_new_note(make_app, owner_dir):
    from litetui.claude_turn import ledger_for, system_prompt_for
    app = make_app()
    ledger = ledger_for(app)
    segment = ledger.select_segment("ws")
    first = system_prompt_for(app, segment)
    _now(owner_dir, NEW_NOW)
    assert system_prompt_for(app, ledger.segment(segment["id"])) == first, "a resume carries the same prefix"
    fresh = ledger.select_segment("ws", new=True)
    renewed = system_prompt_for(app, fresh)
    assert NEW_NOW in renewed and NOW not in renewed


def test_the_non_owner_claude_request_and_prompt_carry_no_owner_text(make_app):
    from litetui import claude_compact
    from litetui.claude_turn import ledger_for, system_prompt_for
    app = make_app(owner=False)
    prompt = system_prompt_for(app, ledger_for(app).select_segment("ws"))
    assert "owner memory folder" not in prompt and "NOW.md" not in prompt
    assert "NOW.md" not in claude_compact.request("")


# -- review round: a NON-owner launch on a busy home still ends in parser.error, as before ------

def _held_home(tmp_path, name="Alpha"):
    import dataclasses
    return ordinary(tmp_path, dataclasses.replace(settings.load(), seat_name=name))


def test_a_busy_agent_home_still_ends_in_a_cli_error_without_owner_mode(tmp_path, cli_env, capsys):
    held = _held_home(tmp_path)
    try:
        with pytest.raises(SystemExit) as exc:
            cli_env("--agent", "Alpha")
        assert exc.value.code == 2 and not _FakeApp.built
        assert "Resource owned or inaccessible" in capsys.readouterr().err
    finally:
        held.release()


def test_a_spawned_seat_on_a_busy_home_still_ends_in_a_cli_error(tmp_path, cli_env, capsys, monkeypatch):
    from litetui import harness
    held = _held_home(tmp_path)
    try:
        monkeypatch.setenv(harness.SPAWN_IDENTITY_MARKER, "1")
        monkeypatch.setattr(harness, "spawned_seat_identity",
                            lambda *a, **k: (held.authority.agent_id, "Alpha", "worker"))
        with pytest.raises(SystemExit) as exc:
            cli_env()
        assert exc.value.code == 2 and not _FakeApp.built
        assert "Resource owned or inaccessible" in capsys.readouterr().err
    finally:
        held.release()


def test_create_agent_over_a_busy_home_still_ends_in_a_cli_error(tmp_path, cli_env, capsys):
    held = _held_home(tmp_path)
    try:
        with pytest.raises(SystemExit) as exc:
            cli_env("--create-agent", "Alpha", "--agent-id", "33333333-3333-4333-8333-333333333333",
                    "--backend", "lmstudio", "--model", "m", "--thinking-level", "low")
        assert exc.value.code == 2 and not _FakeApp.built
    finally:
        held.release()


def test_a_busy_default_home_still_opens_a_generated_one(tmp_path, cli_env, capsys):
    held = ordinary(tmp_path, settings.load())
    try:
        cli_env()
        assert len(_FakeApp.built) == 1
        assert "Default agent is busy" in capsys.readouterr().err
        assert _FakeApp.built[0]["owner_memory_dir"] is None
    finally:
        held.release()


def test_owner_with_create_agent_gets_the_owner_message(tmp_path, owner_dir, cli_env, capsys):
    with pytest.raises(SystemExit) as exc:
        cli_env("--owner", "Alpha", "--memory", str(owner_dir), "--create-agent", "Beta")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--owner opens the agent home by its own name" in err and "--create-agent requires" not in err
    assert not (tmp_path / ".agents").exists()


def test_a_busy_home_on_the_owner_resume_path_gets_the_owner_message(tmp_path, owner_dir, cli_env, capsys):
    held = _held_home(tmp_path)
    try:
        with pytest.raises(SystemExit) as exc:
            cli_env("--owner", "Alpha", "--memory", str(owner_dir), "--convo", "44444444-4444-4444-8444-444444444444")
        assert exc.value.code == 2
        assert "--owner Alpha" in capsys.readouterr().err
    finally:
        held.release()


# -- review round: the 70% boundary and the wording of the SOUL.md claim ------------------------

@pytest.mark.parametrize("used,window,fires", [
    (500, 1000, False), (600, 1000, False), (699, 1000, False), (700, 1000, True), (701, 1000, True),
    (716, 1024, False), (717, 1024, True),
])
def test_the_reminder_boundary_is_seventy_percent_inclusive(make_app, used, window, fires):
    app = make_app()
    system = []

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            app._system = lambda text, *a, **k: system.append(text)
            app.ctx_max = window
            app.ctx_used = used
            await pilot.pause()
    import asyncio
    asyncio.run(body())
    assert (owner_mode.CONTEXT_REMINDER in system) is fires


def _repo_text(*parts):
    from pathlib import Path
    return Path(__file__).resolve().parent.parent.joinpath(*parts).read_text(encoding="utf-8")


def test_nothing_claims_that_editing_soul_always_asks():
    for text in (_repo_text("README.md"), _repo_text("src", "litetui", "prompts", "owner-store.md"),
                 _repo_text("src", "litetui", "tool_policy.py"), _repo_text("CHANGELOG.md")):
        lowered = text.lower()
        assert "editing it still asks" not in lowered and "editing it asks the user first" not in lowered
        assert "an edit to it is still asked about" not in lowered


def test_the_readme_states_the_real_soul_policy_and_the_known_limits():
    text = " ".join(_repo_text("README.md").split()).lower()
    assert "asks only on the strict profile" in text
    assert "matching follows the filesystem" in text
    assert ".owner.lock" in text and "ignore" in text
