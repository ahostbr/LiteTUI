"""Actual role and composer policy are part of native prompt compatibility."""
from uuid import uuid4

import pytest
from _owned_seat import owned_app

from litetui import claude_turn, seat_prompts


def test_restart_role_change_with_same_body_method_changes_revision_and_rotates(tmp_path):
    method = tmp_path / "method.md"
    method.write_text("# POLYMATHIC GAMMA\nSAME METHOD", encoding="utf-8")
    kwargs = {"initial_backend": "claude", "initial_model": "claude-sonnet", "system_prompt": "SAME HEADERLESS BODY",
              "cognitive_file": str(method)}
    first = owned_app(**kwargs, spawn_identity=(str(uuid4()), "builder", "worker"))
    first._materialise_convo()
    ledger = claude_turn.ledger_for(first)
    original = ledger.select_segment(claude_turn.launch_workspace(first))
    old_text = claude_turn.system_prompt_for(first, original)
    ledger.bind_session(original["id"], "old-builder-native-id")
    old = ledger.selected
    old_revision = seat_prompts.revision(first)
    path = first.convo_path
    first.store.release()
    first._agent_session.release()
    critic = owned_app(**kwargs, spawn_identity=(str(uuid4()), "critic", "reviewer"))
    critic._render_resumed = lambda _: None
    critic._system = lambda _: None
    assert critic._resume(path, startup=True)
    assert critic._prompt_snapshot.texts == first._prompt_snapshot.texts
    assert seat_prompts.revision(critic) != old_revision
    with pytest.raises(claude_turn.UncertainDelivery, match="saved system revision differs"):
        claude_turn.system_prompt_for(critic, claude_turn.ledger_for(critic).selected)
    delivery = claude_turn.prepare_input(critic, "NEW CRITIC INPUT", "strict", "typed")
    current = claude_turn.ledger_for(critic)
    assert current.selected["id"] != old["id"]
    assert current.segment(old["id"]) == old
    assert current.segment(old["id"])["system_prompt"] == old_text
    text = claude_turn.system_prompt_for(critic, current.selected)
    assert text.startswith("# CRITIC ROLE") and "# BUILDER ROLE" not in text
    assert delivery["_claude_segment"] == current.selected["id"]
