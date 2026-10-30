"""Owned Claude restart recovery for Rust; providers and process launch are tripwired."""
from __future__ import annotations

import copy
import json

import httpx
import pytest
import pytest_asyncio

from litetui import (
    claude_turn,
    gui_rpc,
    hook_host,
    restored_claude,
    restored_input,
    settings,
    store_identity,
)
from litetui.claude_persistence import ClaudeLedger


@pytest_asyncio.fixture
async def saved_app(make_app, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Provider/process/admission reached by Claude recovery")

    monkeypatch.setattr("platform.node", lambda: "controlled-host")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(hook_host, "enter_conversation", lambda *_: None)
    monkeypatch.setattr(hook_host, "leave_conversation", lambda *_: None)
    monkeypatch.setattr(hook_host, "start_prompt", forbidden)
    settings.save(settings.Settings(backend="claude", default_model="fixture-model",
                                    tools_enabled=False, mcp_enabled=False, skills_enabled=False))
    app = make_app(initial_backend="claude", initial_model="fixture-model", hold_restored_input=True)
    app._system = lambda *_: None
    app._refresh_ctx_label = lambda: None
    app._sync_fleet_identity = lambda: None
    app._render_resumed = lambda *_args, **_kwargs: None
    app._ensure_legacy_store_notice = lambda: None
    app._chat_running = lambda: False
    app._stream = forbidden
    app.conversation = [{"role": "system", "content": "Fixture system"},
                        {"role": "user", "content": "Saved prompt"}]
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    try:
        yield app
    finally:
        await app.client.close()


def seed(app, state="prepared", *, content="UNSENT CLAUDE INPUT", profile="strict", source="harness", operation=None):
    ledger = claude_turn.ledger_for(app)
    segment = ledger.select_segment(claude_turn.launch_workspace(app))
    ledger.fix_system_prompt(segment["id"], "Fixture system",
                             store_identity=store_identity.capture(app), store_injected=True)
    ledger.bind_session(segment["id"], "saved-native-session")
    entry = ledger.prepare(segment["id"], content, profile, source, operation_id=operation)
    if state in ("submitted", "acknowledged", "uncertain"):
        ledger.update_delivery(entry["id"], "submitted")
    if state == "acknowledged":
        ledger.update_delivery(entry["id"], "acknowledged")
    if state == "uncertain":
        ledger.update_delivery(entry["id"], "uncertain")
    return entry


def read(app):
    return gui_rpc.dispatch(app, {"type": "gui.restored_input.list", "session_id": app.convo_id})


def cancel_command(app, entry):
    return {"type": "gui.restored_input.cancel", "id": "cancel-claude",
            "session_id": app.convo_id, "entry_id": entry["id"], "snapshot": read(app)["snapshot"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "submitted", "acknowledged", "uncertain"])
async def test_owned_resume_exposes_held_evidence_without_sending_or_rewriting(saved_app, state):
    app = saved_app
    entry = seed(app, state)
    before = app._claude_ledger.file.read_bytes()
    assert app._resume(app.convo_path, startup=True)
    restored_claude.restore(app)
    assert len(app._pending_input) == 1
    item = app._pending_input[0]
    assert restored_input.held(item)
    assert item["_claude_entry"]["state"] == ("prepared" if state == "prepared" else "uncertain")
    result = read(app)
    row = result["entries"][0]
    assert result["held"] == 1 and row["id"] == entry["id"] and row["provider"] == "claude"
    assert row["can_cancel"] is (state == "prepared")
    assert row["can_continue"] is (state == "prepared")
    assert row["can_reconcile"] is False
    assert row["thread_id"] == "saved-native-session" and row["segment_id"] == entry["segment_id"]
    assert row["uncertain_from"] == ("" if state == "prepared" else "submitted" if state == "uncertain" else state)
    app._flush_pending_input()
    assert not claude_turn.queue_ready(app, item)
    assert not app._deliver_queued_input()
    claude_turn.command(app, "continue")
    assert app._pending_input == [item]
    assert app._claude_ledger.file.read_bytes() == before
    assert restored_input.status(app) == {"policy": "hold", "held": 1}


@pytest.mark.asyncio
async def test_prepared_cancellation_persists_once_and_survives_owned_resume(saved_app):
    app = saved_app
    entry = seed(app)
    uncertain = seed(app, "acknowledged", content="MAY ALREADY HAVE RUN")
    assert app._resume(app.convo_path, startup=True)
    before = app.convo_path.read_bytes()
    command = cancel_command(app, entry)
    result = gui_rpc.dispatch(app, command)
    assert result == {"cancelled": entry["id"], "session_id": app.convo_id}
    assert app.convo_path.read_bytes() == before
    ledger = ClaudeLedger(app.convo_dir)
    segment = ledger.selected
    assert segment is not None
    closed = next(e for e in segment["entries"] if e["id"] == entry["id"])
    assert closed["state"] == "terminal" and closed["stop_reason"] == "user_cancelled_before_send"
    assert read(app)["entries"][0]["id"] == uncertain["id"]
    with pytest.raises(ValueError):
        gui_rpc.dispatch(app, command)
    assert app._resume(app.convo_path, startup=True)
    assert [row["id"] for row in read(app)["entries"]] == [uncertain["id"]]
    assert all(m.get("content") != entry["content"] for m in app.conversation)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["submitted", "acknowledged", "uncertain"])
async def test_uncertain_deliveries_cannot_be_cancelled_or_continued(saved_app, state):
    app = saved_app
    entry = seed(app, state)
    assert app._resume(app.convo_path, startup=True)
    before = app._claude_ledger.file.read_bytes()
    command = cancel_command(app, entry)
    for operation in ("cancel", "continue", "reconcile"):
        command["type"] = f"gui.restored_input.{operation}"
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(app, command)
    assert app._claude_ledger.file.read_bytes() == before and len(app._pending_input) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["snapshot", "session", "id", "queue", "disk", "owner", "busy"])
@pytest.mark.parametrize("action", ["cancel", "resolve"])
async def test_stale_or_foreign_cancellation_cannot_change_saved_delivery(saved_app, fault, action):
    app = saved_app
    entry = seed(app, "prepared" if action == "cancel" else "uncertain")
    assert app._resume(app.convo_path, startup=True)
    command = cancel_command(app, entry)
    command["type"] = f"gui.restored_input.{action}"
    owner = app._agent_session
    if fault in ("snapshot", "session", "id"):
        command[{"session": "session_id", "id": "entry_id"}.get(fault, fault)] = "foreign"
    elif fault == "queue":
        app._pending_input[0]["_claude_entry"]["content"] = "changed"
    elif fault == "disk":
        external = ClaudeLedger(app.convo_dir)
        external.select_segment("foreign workspace", new=True)
    elif fault == "owner":
        app._agent_session = None
    else:
        app._chat_running = lambda: True
    before = app._claude_ledger.file.read_bytes()
    try:
        with pytest.raises(ValueError):
            gui_rpc.dispatch(app, command)
        assert app._claude_ledger.file.read_bytes() == before and len(app._pending_input) == 1
    finally:
        app._agent_session = owner


@pytest.mark.asyncio
@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("action", ["cancel", "resolve"])
async def test_ambiguous_cancellation_latches_uncertainty_and_never_retries(saved_app, monkeypatch, after_write, action):
    app = saved_app
    entry = seed(app, "prepared" if action == "cancel" else "uncertain")
    assert app._resume(app.convo_path, startup=True)
    command = cancel_command(app, entry)
    command["type"] = f"gui.restored_input.{action}"
    save = app._claude_ledger._save
    calls = []
    def failed():
        calls.append("save")
        if after_write:
            save()
        raise OSError("controlled storage ambiguity")
    monkeypatch.setattr(app._claude_ledger, "_save", failed)
    with pytest.raises(OSError):
        gui_rpc.dispatch(app, command)
    assert app._restored_input_error and len(app._pending_input) == 1
    with pytest.raises(ValueError):
        read(app)
    with pytest.raises(ValueError):
        gui_rpc.dispatch(app, command)
    assert calls == ["save"]


@pytest.mark.asyncio
async def test_ordinary_python_policy_does_not_restore_or_change_claude_ledger(saved_app):
    app = saved_app
    seed(app)
    before = app._claude_ledger.file.read_bytes()
    app._hold_restored_input = False
    assert app._resume(app.convo_path, startup=True)
    assert app._pending_input == []
    assert app._claude_ledger.file.read_bytes() == before


@pytest.mark.asyncio
async def test_mixed_provider_holds_keep_order_and_cancel_only_selected_journal(saved_app):
    from test_rust_restored_input import seed as seed_codex

    app = saved_app
    codex = seed_codex(app)
    claude = seed(app)
    assert app._resume(app.convo_path, startup=True)
    rows = read(app)["entries"]
    assert [r["id"] for r in rows] == [codex["id"], claude["id"]]
    before = app.convo_path.read_bytes()
    gui_rpc.dispatch(app, cancel_command(app, claude))
    assert app.convo_path.read_bytes() == before
    assert [r["id"] for r in read(app)["entries"]] == [codex["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["duplicate", "segment", "state", "content", "budget"])
async def test_malformed_saved_ledger_refuses_restore_before_queue_changes(saved_app, fault):
    app = saved_app
    entry = seed(app)
    ledger = app._claude_ledger
    document = json.loads(ledger.file.read_text())
    selected = document["segments"][entry["segment_id"]]
    if fault == "duplicate":
        selected["entries"].append(copy.deepcopy(selected["entries"][0]))
    elif fault == "segment":
        selected["entries"][0]["segment_id"] = "foreign"
    elif fault == "state":
        selected["entries"][0]["state"] = "invented"
    elif fault == "content":
        selected["entries"][0]["content"] = "x" * 65537
    else:
        document["padding"] = "x" * (8 * 1024 * 1024)
    ledger.file.write_text(json.dumps(document))
    before = ledger.file.read_bytes()
    with pytest.raises(ValueError):
        restored_claude.restore(app)
    assert app._pending_input == [] and ledger.file.read_bytes() == before


@pytest.mark.asyncio
async def test_ledger_document_is_detached_and_full_snapshot_catches_hidden_change(saved_app):
    app = saved_app
    entry = seed(app)
    assert app._resume(app.convo_path, startup=True)
    ledger = app._claude_ledger
    detached = ledger.document
    detached["segments"].clear()
    assert ledger.selected["entries"]
    command = cancel_command(app, entry)
    # A change outside the visible row must still invalidate its confirmation.
    ledger.mark_store_notice_delivered()
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(app, command)
    assert len(app._pending_input) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["submitted", "acknowledged", "uncertain"])
async def test_resolution_is_selected_durable_and_never_claims_provider_effects(saved_app, state):
    app = saved_app
    prepared = seed(app, content="KEEP PREPARED")
    target = seed(app, state, content="UNCERTAIN TARGET")
    other = seed(app, "submitted", content="KEEP UNCERTAIN")
    assert app._resume(app.convo_path, startup=True)
    before = app.convo_path.read_bytes()
    authority = (app._active_tool_profile, app._hook_turn_id, getattr(app, "_gui_operation_id", None))
    command = cancel_command(app, target)
    command["type"] = "gui.restored_input.resolve"
    assert read(app)["entries"][1]["can_resolve"]
    result = await gui_rpc.async_dispatch(app, command)
    assert result == {"resolved": target["id"], "session_id": app.convo_id,
                      "replayed": False, "effects_verified": False}
    assert app.convo_path.read_bytes() == before
    assert authority == (app._active_tool_profile, app._hook_turn_id, getattr(app, "_gui_operation_id", None))
    closed = next(e for e in app._claude_ledger.selected["entries"] if e["id"] == target["id"])
    assert closed["state"] == "terminal" and closed["stop_reason"] == "user_resolved_without_replay"
    assert closed["content"] == target["content"] and closed["uncertain_from"]
    assert [r["id"] for r in read(app)["entries"]] == [prepared["id"], other["id"]]
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command)
    assert app._resume(app.convo_path, startup=True)
    assert [r["id"] for r in read(app)["entries"]] == [prepared["id"], other["id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_resolution_cannot_close_prepared_or_another_provider(saved_app, provider):
    from test_rust_restored_input import seed as seed_codex

    app = saved_app
    target = seed(app) if provider == "claude" else seed_codex(app, "uncertain")
    assert app._resume(app.convo_path, startup=True)
    command = cancel_command(app, target)
    command["type"] = "gui.restored_input.resolve"
    before = app.convo_path.read_bytes()
    assert not read(app)["entries"][0].get("can_resolve", False)
    with pytest.raises(ValueError, match="Only an uncertain Claude"):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_path.read_bytes() == before and len(app._pending_input) == 1
