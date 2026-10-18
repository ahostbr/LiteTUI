"""Native restart/continuity regressions against real leased app/ledger paths."""
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from _owned_seat import owned_app

from litetui import claude_turn, seat_prompts
from litetui import prompt_overrides as prompts
from litetui.store_identity import capture


def selected_app(tmp_path, **kwargs):
    role = tmp_path / "role-source.md"
    method = tmp_path / "method-source.md"
    role.write_text("CURRENT ROLE BODY", encoding="utf-8")
    method.write_text("# POLYMATHIC GAMMA\nCURRENT METHOD", encoding="utf-8")
    return owned_app(initial_backend="claude", initial_model="claude-sonnet", role_prompt_file=str(role),
                     cognitive_file=str(method), **kwargs), role, method


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_unchanged_selected_policy_restart_holds_unversioned_wrong_native_system(tmp_path, monkeypatch, queued):
    first, role, method = selected_app(tmp_path)
    first._materialise_convo()
    ledger = claude_turn.ledger_for(first)
    segment = ledger.select_segment(claude_turn.launch_workspace(first))
    old_system = "# WORKER ROLE — Worker Mode\n# POLYMATHIC GAMMA — Worker Mode\nOLD WRONG SCAFFOLD"
    ledger.fix_system_prompt(segment["id"], old_system, store_identity=capture(first), store_injected=True)
    ledger.bind_session(segment["id"], "old-native-id")
    entry = ledger.prepare(segment["id"], "OLD QUEUED", "strict", "typed") if queued else None
    path, cid = first.convo_path, first.convo_id
    before = ledger.file.read_bytes()
    original = ledger.selected
    first.store.release()
    first._agent_session.release()
    resumed = owned_app(initial_backend="claude", initial_model="claude-sonnet", role_prompt_file=str(role),
                        cognitive_file=str(method))
    resumed._render_resumed = lambda _: None
    resumed._system = lambda _: None
    assert resumed._resume(path, startup=True)
    native = claude_turn.ledger_for(resumed)
    # No file edit after restart: this is the previously missing load-bearing case.
    assert resumed._prompt_revision_at_launch == seat_prompts.revision(resumed)
    opened = AsyncMock()
    query = AsyncMock()
    monkeypatch.setattr(resumed.backend, "open_session", opened)
    resumed.backend.session = NS(query=query, effort=None)
    resumed.backend.segment_id = segment["id"]
    with pytest.raises(claude_turn.UncertainDelivery, match="/claude new"):
        claude_turn.prepare_input(resumed, "MUST NOT PREPARE", "strict", "typed")
    with pytest.raises(claude_turn.UncertainDelivery, match="/claude new"):
        await claude_turn.session_for(resumed, resumed.backend, native.selected, None)
    with pytest.raises(claude_turn.UncertainDelivery, match="/claude new"):
        claude_turn.system_prompt_for(resumed, native.selected)
    if entry:
        item = {"content": entry["content"], "_claude_entry": entry, "_claude_segment": segment["id"], "_claude_conversation": cid}
        assert claude_turn.queue_ready(resumed, item) is False
        with pytest.raises(claude_turn.UncertainDelivery, match="/claude new"):
            claude_turn.accept_input(resumed, item)
    assert native.selected == original
    assert native.file.read_bytes() == before
    opened.assert_not_called()
    query.assert_not_called()
    # The transport substitute is not a live native process to shut down.
    resumed.backend.session = None


@pytest.mark.asyncio
async def test_two_system_edits_carry_one_recorded_history_snapshot_and_root_seed(tmp_path, monkeypatch):
    app, _, _ = selected_app(tmp_path)
    app._materialise_convo()
    app._system = lambda _: None
    app._seat_started = True
    ledger = claude_turn.ledger_for(app)
    initial = ledger.select_segment(claude_turn.launch_workspace(app), seed="ORIGINAL-BASE-SEED")
    claude_turn.system_prompt_for(app, initial)
    initial_saved = ledger.selected
    app.conversation += [{"role": "user", "content": "QUESTION-A"},
                         {"role": "assistant", "content": "ANSWER-A", "claude_native": {
                             "activities": [{"id": "ACTIVITY-A", "kind": "tool", "text": "recorded activity"}]}}]
    emitted = []

    async def open_session(segment, model, *, system_prompt, **kwargs):
        emitted.append(Path(system_prompt["path"]).read_text(encoding="utf-8"))
        return NS(effort=None)

    monkeypatch.setattr(app.backend, "open_session", open_session)
    previous = None
    for index in (1, 2):
        (app._agent_session.memory_root / "prompts" / "role.md").write_text(f"SYSTEM-EDIT-{index}", encoding="utf-8")
        prompts.refresh(app)
        delivery = claude_turn.prepare_input(app, f"QUESTION-{index}", "strict", "typed")
        await claude_turn.session_for(app, app.backend, ledger.selected, None)
        text = emitted[-1]
        assert text.count("ORIGINAL-BASE-SEED") == 1
        assert text.count("QUESTION-A") == 1 and text.count("ACTIVITY-A") == 1
        assert text.count("End each FINAL answer with <recap>") == 1
        assert ledger.segment(initial["id"]) == initial_saved
        if previous:
            assert ledger.segment(previous["id"]) == previous
            assert text.count("QUESTION-1") == 1 and text.count("ACTIVITY-1") == 1
        ledger.update_delivery(delivery["_claude_entry"]["id"], "terminal", stop_reason="fixture_completed")
        previous = ledger.selected
        app.conversation += [{"role": "user", "content": f"QUESTION-{index}"},
                             {"role": "assistant", "content": f"ANSWER-{index}", "claude_native": {
                                 "activities": [{"id": f"ACTIVITY-{index}", "kind": "tool"}]}}]
    assert len(emitted) == 2
