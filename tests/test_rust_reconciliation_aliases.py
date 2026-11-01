"""Canonical recovery persists exact shared journal objects without adopting copies."""
from __future__ import annotations

import copy

import pytest
from test_rust_queue_reconciliation import saved_app as _saved_app
from test_rust_queue_reconciliation import setup

from litetui import codex_steering, gui_rpc, restored_input
from litetui.conversation import ConversationRepository

saved_app = _saved_app
pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("failure", [False, True], ids=["all-aliases", "partial-write-failure"])
async def test_reconciliation_saves_exact_shared_rows_and_never_replays_partial_write(saved_app, monkeypatch, failure):
    app = saved_app
    command, calls, events, origins = setup(app, monkeypatch)
    item = app._pending_input[0]
    native, entries, original_index = restored_input._records(app.conversation)["saved-input"]
    original = app.conversation[original_index]
    copied = {"role": "assistant", "content": "Separate historical copy", "provider_metadata": copy.deepcopy(original["provider_metadata"])}
    app._append(copied)
    app._append({"role": "assistant", "content": "Exact live journal alias", "provider_metadata": original["provider_metadata"]})
    alias_index = len(app.conversation) - 1
    view = restored_input.inspect(app, app.convo_id)
    command["snapshot"] = view["snapshot"]
    before = copy.deepcopy(app.conversation)
    actual_save = codex_steering.save_at
    saved = []
    def save(owner, index):
        saved.append(index)
        if failure and index == alias_index:
            raise OSError("Controlled second aliased-row write failure")
        actual_save(owner, index)
    monkeypatch.setattr(codex_steering, "save_at", save)
    if failure:
        with pytest.raises(OSError, match="second aliased-row"):
            await gui_rpc.async_dispatch(app, command)
        assert app._restored_input_error and app._pending_input == [item]
        assert len(app.conversation) == len(before)
        with pytest.raises(ValueError, match="durably saved"):
            await gui_rpc.async_dispatch(app, command)
    else:
        result = await gui_rpc.async_dispatch(app, command)
        assert result["reconciled"] and not app._pending_input
        assert native["state"] == "accepted"
        assert ConversationRepository.read(app.convo_path)[1] == app.conversation
        assert len(app.conversation) == len(before) + 1
    assert saved == [original_index, alias_index]
    assert app.conversation[original_index] is original
    assert app.conversation[original_index]["provider_metadata"]["steering"] is entries
    assert app.conversation[alias_index]["provider_metadata"]["steering"] is entries
    assert copied == before[-2] and copied["provider_metadata"]["steering"][0]["state"] == "uncertain"
    assert [row["content"] for row in app.conversation[:len(before)]] == [row["content"] for row in before]
    assert not events and not origins and all(method in {"start", "thread/read"} for method, _ in calls)
