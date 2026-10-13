"""A launch cannot silently change the store of a saved conversation."""
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import owner_mode
from litetui.agent_launch_context import ordinary
from litetui.claude_turn import ledger_for, system_prompt_for
from litetui.settings import Settings


@pytest.fixture
def build(tmp_path):
    apps = []

    def make(memory, backend="lmstudio"):
        session = ordinary(tmp_path, Settings(seat_name="Alpha", backend=backend,
                                              default_model="fixture-model"))
        app = app_mod.LiteTUI(agent_session=session, owner_memory_dir=memory)
        apps.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._cli_convo_id = None
        (session.memory_root / "soul.md").write_text("HOME-BODY", encoding="utf-8")
        return app

    yield make
    for app in reversed(apps):
        app.store.release()
        app._agent_session.release()


def folder(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    for file in owner_mode.OWNER_FILES:
        (root / file).write_text(f"OWNER-BODY-{name}-{file}", encoding="utf-8")
    return root


def save(app, backend):
    app._materialise_convo()
    app._request_messages()
    if backend == "claude":
        system_prompt_for(app, ledger_for(app).select_segment(str(Path.cwd())))
    app._append({"role": "user", "content": "saved turn"})
    path = app.convo_path
    app.store.release()
    app._agent_session.release()
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
@pytest.mark.parametrize("change", ["ordinary-to-owner", "owner-to-ordinary", "other-folder"])
@pytest.mark.parametrize("via_cli", [False, True])
async def test_mismatched_resume_refuses_before_adoption(tmp_path, build, backend, change, via_cli):
    original = None if change == "ordinary-to-owner" else folder(tmp_path, "widgets")
    target = None if change == "owner-to-ordinary" else folder(tmp_path, "gadgets")
    path = save(build(original, backend), backend)
    transcript = path.read_bytes()
    ledger_path = path.parent / "claude_ledger.json"
    ledger_bytes = ledger_path.read_bytes() if ledger_path.exists() else None
    resumed = build(target, backend)
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        before = resumed.convo_path
        resumed._cli_convo_id = path.parent.name
        accepted = resumed._resume_cli_conversation() if via_cli else resumed._resume(path)
        assert not accepted, "store mismatch was silently resumed"
        if via_cli:
            assert "Store mismatch" in resumed._cli_launch_error
        assert resumed.convo_path == before
        assert len(said) == 1 and "store mismatch" in said[0].lower()
        assert "--convo" in said[0] and path.parent.name in said[0]
        if original is None:
            assert "without --owner and --memory" in said[0]
        else:
            assert "--owner Alpha" in said[0] and "--memory" in said[0]
            assert str(original) in said[0]
    assert path.read_bytes() == transcript
    if ledger_bytes is not None:
        assert ledger_path.read_bytes() == ledger_bytes


@pytest.mark.parametrize("change", ["ordinary-to-owner", "owner-to-ordinary", "other-folder"])
def test_recorded_claude_prompt_is_refused_not_rewritten(tmp_path, build, change):
    original = None if change == "ordinary-to-owner" else folder(tmp_path, "widgets")
    target = None if change == "owner-to-ordinary" else folder(tmp_path, "gadgets")
    app = build(original)
    app._materialise_convo()
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(Path.cwd()))
    prompt = system_prompt_for(app, segment)
    before = ledger.file.read_bytes()
    app._owner_memory_dir = target
    with pytest.raises(owner_mode.OwnerModeError, match="[Ss]tore mismatch"):
        system_prompt_for(app, segment)
    assert ledger.segment(segment["id"])["system_prompt"] == prompt
    assert ledger.file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
async def test_same_owner_resume_preserves_both_snapshots(tmp_path, build, backend):
    memory = folder(tmp_path, "widgets")
    first = build(memory, backend)
    path = save(first, backend)
    saved = first.conversation[0]["content"]
    native = ledger_for(first).selected if backend == "claude" else None
    (memory / "NOW.md").write_text("CHANGED-NOW", encoding="utf-8")
    resumed = build(memory, backend)
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        assert resumed._request_messages()[0]["content"] == saved
        if native:
            assert system_prompt_for(resumed, ledger_for(resumed).selected) == native["system_prompt"]


@pytest.mark.asyncio
async def test_saved_native_prompt_is_checked_even_when_message_zero_matches(tmp_path, build):
    from litetui.textfmt import memory_prompt

    memory = folder(tmp_path, "widgets")
    first = build(None)
    first._materialise_convo()
    first._request_messages()
    ledger = ledger_for(first)
    segment = ledger.select_segment(str(Path.cwd()))
    ledger.fix_system_prompt(segment["id"], memory_prompt(first.convo_id, memory, owner=True))
    path = save(first, "lmstudio")
    before = ledger.file.read_bytes()
    resumed = build(None)
    said = []
    resumed._system = said.append
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert not resumed._resume(path)
        assert len(said) == 1 and "Store mismatch" in said[0]
        assert "--memory" in said[0]
    assert ledger.file.read_bytes() == before


@pytest.mark.parametrize("malformed", ["missing-address", "conflicting-addresses", "mixed-snapshot"])
def test_ambiguous_stored_identity_refuses_without_guessing(tmp_path, malformed):
    from litetui.appsvc import STORE_HEADER
    from litetui.store_identity import require_store_match
    from litetui.textfmt import memory_prompt

    root = tmp_path / "widgets"
    prompt = memory_prompt("saved", root, owner=True)
    if malformed == "missing-address":
        prompt = "## Your conversation store (owner memory folder)\nunknown"
    elif malformed == "conflicting-addresses":
        prompt += memory_prompt("saved", tmp_path / "other", owner=True)
    else:
        prompt += f"\n\n{STORE_HEADER}\n### memory.md (current contents)\nhome"
    with pytest.raises(owner_mode.OwnerModeError, match="omit --convo"):
        require_store_match(prompt, root, agent_name="Alpha", convo_id="saved")
