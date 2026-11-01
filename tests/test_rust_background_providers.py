"""Captured provider journals use real source ownership with controlled replies."""
import asyncio
from typing import Any

import pytest
from test_rust_background_input import app as _app
from test_rust_background_input import armed as _armed
from test_rust_background_input import settle_inputs
from test_rust_background_input import switch_app as _switch_app

from litetui import background_input as work
from litetui.codex_steering import HostSteering, RejectedRequest
from litetui.event_origin import EventOrigin
from litetui.model_transport import ProviderError

app, armed, switch_app = _app, _armed, _switch_app


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", ["codex"], indirect=True)
@pytest.mark.parametrize("outcome", ["accepted", "next_turn", "uncertain", "reconciled_current", "reconciled_recovered"])
async def test_actual_codex_steering_binds_work_to_exact_parent_and_native_receipt(armed, monkeypatch, outcome):
    entered, delivered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = armed._stream_worker
    class Server:
        async def request(self, method, params):
            assert method == "turn/steer"
            native = armed._pending_input[0]["_codex_entry"]
            assert native["state"] == "sending"
            assert next(iter(work.bridge(armed).inputs.values())).state == "queued"
            delivered.set()
            if outcome == "next_turn":
                raise RejectedRequest("Known pre-injection rejection", -32600)
            if outcome == "uncertain" or outcome.startswith("reconciled_"):
                raise ProviderError("Controlled lost reply")
            return {"turnId": "native-active"}
    async def stream(origin):
        metadata = {"provider": "codex", "steering": []}
        armed.conversation[3]["provider_metadata"] = metadata
        armed._edit(3, "Controlled native parent metadata")
        lane = HostSteering(armed, Server(), "native-thread", "native-active", metadata,
                           lambda: armed._edit(3, "Controlled native delivery state"))
        child = armed.run_worker(lane.run(), group="codex-steering", exit_on_error=False)
        entered.set()
        try:
            await finish.wait()
            await original(origin)
        finally:
            child.cancel()
            await asyncio.gather(child.wait(), return_exceptions=True)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    parent = armed._stream(origin=EventOrigin(session_id=armed.convo_id, operation_id="provider-parent", source="rpc"))
    await entered.wait()
    async def output():
        return "Exact autonomous completion for native steering"
    armed._start_background("bash", {"command": "fixture"}, output())
    await next(iter(work.bridge(armed).producers.values())).worker.wait()
    await asyncio.sleep(0.15)
    for child in list(armed.workers):
        if child.group == "codex-steering" and child.is_finished:
            await child.wait()
    queued_native = armed._pending_input[0].get("_codex_entry") if armed._pending_input else None
    assert queued_native is None or queued_native["state"] != "denied", queued_native
    await asyncio.wait_for(delivered.wait(), 3)
    await asyncio.sleep(0.05)
    entry = next(iter(work.bridge(armed).inputs.values()))
    native = entry.item["_codex_entry"]
    finish.set()
    await parent.wait()
    if outcome == "accepted":
        assert entry.admission is not None
        await settle_inputs(armed, 1)
        assert entry.admission["mode"] == "in_turn" and entry.worker is parent
        assert entry.admission["provider_delivery"] == {"id": native["id"], "state": "accepted"}
        assert entry.admission["origin"]["operation_id"] == "provider-parent"
        rows = [row for row in armed.conversation if row.get("work_input_id") == entry.ref["input_id"]]
        assert len(rows) == 1 and rows[0]["codex_delivery"]["id"] == native["id"]
        assert not armed._pending_input
    elif outcome == "next_turn":
        assert entry.state == "queued" and entry.admission is None
        assert armed._pending_input == [entry.item] and native["state"] == "next_turn"
        monkeypatch.setattr(armed, "_stream_worker", original)
        armed._flush_pending_input()
        await settle_inputs(armed, 1)
        admitted = work.bridge(armed).inputs[entry.ref["input_id"]].admission
        assert admitted is not None
        assert admitted["mode"] == "new_turn"
        assert admitted["provider_delivery"] == {"id": native["id"], "state": "pending"}
        assert sum(row.get("codex_delivery", {}).get("id") == native["id"] for row in armed.conversation) == 1
    else:
        assert entry.state == "uncertain" and entry.admission is None
        assert armed._pending_input == [entry.item] and work.bridge(armed).error is not None
    assert native["work_input_id"] == entry.ref["input_id"]
    assert native["work_payload_sha256"] == entry.ref["payload_sha256"]
    if outcome.startswith("reconciled_"):
        from types import SimpleNamespace

        from test_rust_background_input import command

        from litetui import codex_steering, gui_rpc, restored_input
        if outcome == "reconciled_recovered":
            armed._work_test_shutdown = await work.settle_before_shutdown(armed)
            armed._native_work_bridge = None  # Restart loses live ownership; only journals survive.
            armed._pending_input.clear()
            codex_steering.restore_queue(armed)
            descriptor = work.capture(armed, command(armed))
            work.activate(armed, command(armed, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
            entry = work.bridge(armed).inputs[entry.ref["input_id"]]
            assert entry.recovered and entry.producer.worker is None
        calls = []
        async def start():
            calls.append("start")
        async def request(method, params):
            calls.append(method)
            assert method == "thread/read"
            return {"thread": {"id": native["threadId"], "turns": [{"id": native["turnId"],
                "items": [{"type": "userMessage", "clientId": native["id"]}]}]}}
        monkeypatch.setattr(armed.backend, "app_server", SimpleNamespace(start=start, request=request))
        view = restored_input.inspect(armed, armed.convo_id)
        assert view["held"] == 1 and view["entries"][0]["can_reconcile"]
        operation = "explicit-native-history-check"
        armed._work_test_reconciliations = [{"instance": work.bridge(armed).instance,
            "operation_id": operation, "delivery_id": native["id"], "input_id": entry.ref["input_id"],
            "journal_instance": entry.epoch["instance"], "payload_sha256": entry.ref["payload_sha256"]}]
        result = await gui_rpc.async_dispatch(armed, {"type": "gui.restored_input.reconcile", "id": operation,
            "session_id": armed.convo_id, "snapshot": view["snapshot"], "entry_id": native["id"]})
        assert result["reconciled"] and result["work_reconciliation"]["type"] == "work_reconciled"
        assert result["work_reconciliation"]["reconciliation"]["journal_instance"] == native["work_instance"]
        assert entry.state == "reconciled" and entry.admission is None
        assert work.prompt_refusal(armed) and work.bridge(armed).pending_readbacks
        with pytest.raises(ValueError):
            work.reserve(armed)
        # An ordinary F7 refresh is a read, never authority to resume admission.
        generic = await gui_rpc.async_dispatch(armed, {"type": "gui.restored_input.list",
            "id": "ordinary-refresh", "session_id": armed.convo_id})
        assert generic["held"] == 0 and work.prompt_refusal(armed)
        from litetui import rpc
        marker = {"instance": work.bridge(armed).instance, "input_id": entry.ref["input_id"],
                  "operation_id": operation, "delivery_id": native["id"]}
        read = {"type": "gui.restored_input.list", "id": "exact-reconciliation-readback",
                "session_id": armed.convo_id, "work_reconciliation": marker}
        with pytest.raises(ValueError, match="exact pending"):
            work.prepare_reconciliation_readback(armed, {**read,
                "work_reconciliation": {**marker, "operation_id": "foreign-operation"}}, dict(generic))
        actual_emit = rpc.rpc_emit
        from textual.worker import WorkerFailed
        def interrupted_publication(row):
            if row.get("id") == read["id"]:
                raise OSError("Controlled response publication failure")
            return actual_emit(row)
        monkeypatch.setattr(rpc, "rpc_emit", interrupted_publication)
        rpc._dispatch(armed, read)
        failed = await asyncio.wait_for(asyncio.gather(*(w.wait() for w in armed.workers
            if w.group == "gui-management"), return_exceptions=True), 3)
        assert len(failed) == 1 and isinstance(failed[0], WorkerFailed)
        assert work.bridge(armed).pending_readbacks and work.prompt_refusal(armed)
        observed = []
        def publish(row):
            if row.get("id") == read["id"]:
                assert row["ok"] is True, row
                receipt = row["result"]["work_reconciliation_readback"]
                assert receipt["snapshot"] == row["result"]["snapshot"]
                assert receipt["event_ledger_sha256"] == result["work_reconciliation"]["ledger_sha256"]
                assert all(receipt[key] == value for key, value in marker.items())
                assert work.prompt_refusal(armed) and work.bridge(armed).pending_readbacks
                with pytest.raises(ValueError):
                    work.reserve(armed)
                observed.append(receipt)
            return actual_emit(row)
        monkeypatch.setattr(rpc, "rpc_emit", publish)
        rpc._dispatch(armed, read)
        await asyncio.wait_for(asyncio.gather(*(w.wait() for w in armed.workers if w.group == "gui-management" and not w.is_finished)), 3)
        assert len(observed) == 1 and not work.bridge(armed).pending_readbacks
        assert work.bridge(armed).error is None and work.prompt_refusal(armed) is None
        reservation = work.reserve(armed)
        work.release(armed, reservation)
        assert calls == ["start", "thread/read", "thread/read"] and not armed._pending_input
        assert len(armed._rewrite_test_requests) == 1  # Original parent only; reconciliation never starts a turn.
        assert sum(row.get("codex_delivery", {}).get("id") == native["id"] for row in armed.conversation) == 1
        armed._work_test_shutdown = await work.settle_before_shutdown(armed)


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", ["codex"], indirect=True)
async def test_codex_async_question_owns_actual_question_worker_and_original_delivery(armed, monkeypatch):
    from types import SimpleNamespace

    from litetui import question_result
    from litetui.codex_async_questions import AsyncQuestions
    metadata: dict[str, Any] = {"provider": "codex", "app_server_thread_id": "native-thread"}
    armed.conversation[3]["provider_metadata"] = metadata
    armed._edit(3, "Controlled pending native question")
    transport = SimpleNamespace(app=armed, server=SimpleNamespace(), thread_id="native-thread", turn_id="native-turn")
    manager = AsyncQuestions(transport)
    async def answer(name, _args):
        assert name == "ask_user_question"
        records = list(work.bridge(armed).producers.values())
        assert len(records) == 1 and records[0].ref["kind"] == "provider_question"
        question_result.publish({"action": "submit", "questions": [{"answered": True,
            "selected": [0], "options": [{"title": "Exact answer"}], "note": "Original note"}]})
        return "Controlled tool answer", True
    monkeypatch.setattr(armed, "_execute_tool", answer)
    manager.open({"id": "native-question", "questions": [{"title": "Which option?", "options": ["Exact answer"]}]}, metadata, 3)
    record = next(iter(work.bridge(armed).producers.values()))
    await record.worker.wait()
    await record.observer
    entry = next(iter(work.bridge(armed).inputs.values()))
    native = entry.item["_codex_entry"]
    assert record.ref["object_id"] == native["id"]
    assert entry.ref["source"] == "codex-question" and record.phase == "retired"
    assert metadata["async_questions"][0]["state"] == "answered"
    assert armed._pending_input == [entry.item]
    armed._flush_pending_input()
    await settle_inputs(armed, 1)
    assert entry.admission["provider_delivery"] == {"id": native["id"], "state": "pending"}
    assert sum(row.get("codex_delivery", {}).get("id") == native["id"] for row in armed.conversation) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", ["claude"], indirect=True)
async def test_captured_claude_completion_preserves_exact_prepared_journal_id(armed):
    from litetui import claude_turn
    async def output():
        return "Original Claude background completion"
    armed._start_background("bash", {"command": "fixture"}, output())
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    ledger = claude_turn.ledger_for(armed)
    pending = ledger.pending(ledger.selected["id"])
    assert len(pending) == 1 and pending[0]["content"] == entry.payload["content"]
    assert entry.admission["provider"] == "claude"
    assert entry.admission["provider_delivery"] == {"id": pending[0]["id"], "state": "prepared"}
    assert sum(row.get("claude_delivery", {}).get("id") == pending[0]["id"] for row in armed.conversation) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("armed", ["claude"], indirect=True)
@pytest.mark.parametrize("materialized", [False, True])
async def test_active_work_keeps_explicit_claude_f7_recovery_owner_and_no_duplicate_append(armed, materialized):
    from litetui import claude_turn, gui_rpc, restored_claude, restored_input
    ledger = claude_turn.ledger_for(armed)
    segment = ledger.select_segment(claude_turn.launch_workspace(armed))
    native = ledger.prepare(segment["id"], "Original prepared Claude input", "strict", "harness")
    if materialized:
        armed._append({"role": "user", "content": native["content"], "claude_delivery": {
            "id": native["id"], "segment_id": segment["id"], "state": "prepared"}})
    restored_claude.restore(armed)
    view = restored_input.inspect(armed, armed.convo_id)
    assert view["held"] == 1 and view["entries"][0]["can_continue"]
    result = await gui_rpc.async_dispatch(armed, {"type": "gui.restored_input.continue", "id": "explicit-claude-recovery",
        "session_id": armed.convo_id, "snapshot": view["snapshot"], "entry_id": native["id"]})
    assert result["turn"] == "accepted"
    result = await gui_rpc.async_dispatch(armed, {"type": "gui.restored_input.settled", "id": "read-claude-recovery",
        "session_id": armed.convo_id, "operation_id": "explicit-claude-recovery", "entry_id": native["id"]})
    assert result["continuation_state"] == "prepared"  # Controlled worker performs no native SDK send.
    assert sum(row.get("claude_delivery", {}).get("id") == native["id"] for row in armed.conversation) == 1
    assert not work.bridge(armed).inputs and not work.bridge(armed).producers
    assert len(armed._rewrite_test_requests) == 1
    origin = armed._rewrite_test_requests[0][0]
    assert origin.operation_id == "explicit-claude-recovery" and origin.source == "harness"
