"""Captured child wake reads already-committed receipts; it never appends again."""
import asyncio
import copy

import pytest
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import settle_inputs
from test_rust_background_input import switch_app as _switch_app

from litetui import background_input as work
from litetui.agent_parent_delivery import apply_receipts, receipt_message
from litetui.agent_parent_wake import _committed_wake, wake_parent
from litetui.agent_receipts import ParentReceipts
from litetui.turn_deferral import STREAM_DEFERRED

app = _owned_app
armed = _armed
switch_app = _switch_app

pytestmark = pytest.mark.asyncio
PARENT = "controlled-parent"


def saved(armed, tmp_path, count=2):
    receipts = ParentReceipts(tmp_path / "child-receipts.sqlite")
    events = [{"completion_id":f"completion-{n}", "result":{"child_id":f"child-{n}",
        "conversation_id":f"child-chat-{n}", "status":"completed", "summary":f"exact child result {n}",
        "cleanup":{"state":"confirmed"}, "evidence":[]}} for n in range(count)]
    for event in events:
        receipts.accept_for_conversation(PARENT, armed.convo_id, event)
    assert apply_receipts(armed, parent=PARENT, receipts=receipts) == [e["completion_id"] for e in events]
    return receipts, events


async def wake(armed, receipts):
    worker = armed.run_worker(wake_parent(armed, parent=PARENT, receipts=receipts), group="child-wake", exit_on_error=False)
    return await worker.wait()


async def test_committed_receipts_wake_once_with_exact_payload_and_no_second_user_append(armed, tmp_path):
    receipts, events = saved(armed, tmp_path)
    before = copy.deepcopy(armed.conversation)
    raw = armed.convo_path.read_bytes()
    ids = [event["completion_id"] for event in events]
    assert await wake(armed, receipts) == ids
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.state == "settled" and entry.receipt["outcome"] == "completed"
    assert entry.payload["delivery"] == "committed_history" and "content" not in entry.payload
    assert entry.payload["history"]["receipt_ids"] == ids
    assert entry.payload["history"]["transcript_sha256"] == work.digest(raw)
    assert entry.payload["history"]["context_sha256"] == work.digest(work.encoded(before))
    assert entry.admission["mode"] == "new_turn" and entry.admission["origin"]["source"] == "child-result"
    assert entry.producer.ref["kind"] == "child_result"
    assert armed.conversation[:-1] == before and len(armed._rewrite_test_requests) == 1
    for event in events:
        assert armed.conversation.count(receipt_message(event)) == 1
    assert not receipts.pending_wakes(PARENT, armed.convo_id) and not receipts.uncertain_wakes(PARENT, armed.convo_id)
    assert await wake(armed, receipts) == []
    assert len(armed._rewrite_test_requests) == 1


async def test_pre_generation_deferral_releases_exact_claim_without_appending(armed, tmp_path, monkeypatch):
    receipts, events = saved(armed, tmp_path, 1)
    before = armed.convo_path.read_bytes()
    async def deferred(_origin):
        return STREAM_DEFERRED
    monkeypatch.setattr(armed, "_stream_worker", deferred)
    assert await wake(armed, receipts) == []
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.receipt["outcome"] == "deferred"
    assert armed.convo_path.read_bytes() == before
    assert receipts.pending_wakes(PARENT, armed.convo_id) == events
    assert not receipts.uncertain_wakes(PARENT, armed.convo_id)


async def test_stale_prepared_context_refuses_before_claim_and_never_reintroduces_history(armed, tmp_path):
    receipts, events = saved(armed, tmp_path, 1)
    _payload, _validate, claim = _committed_wake(armed, parent=PARENT, receipts=receipts)
    armed._append({"role":"user", "content":"new operator input"})
    with pytest.raises(ValueError, match="history changed"):
        claim()
    assert receipts.pending_wakes(PARENT, armed.convo_id) == events
    assert not receipts.uncertain_wakes(PARENT, armed.convo_id)
    assert not work.bridge(armed).inputs


async def test_pending_receipt_drift_refuses_before_claim(armed, tmp_path):
    receipts, events = saved(armed, tmp_path, 1)
    _payload, _validate, claim = _committed_wake(armed, parent=PARENT, receipts=receipts)
    receipts.claim_wake(PARENT, armed.convo_id)
    with pytest.raises(ValueError, match="receipts changed"):
        claim()
    assert receipts.uncertain_wakes(PARENT, armed.convo_id) == [events[0]["completion_id"]]
    assert not work.bridge(armed).inputs


@pytest.mark.parametrize("fault", ["claim", "finish"])
async def test_ambiguous_claim_or_finish_stays_uncertain_without_replay(armed, tmp_path, monkeypatch, fault):
    from textual.worker import WorkerFailed
    receipts, events = saved(armed, tmp_path, 1)
    original = receipts.claim_wake
    if fault == "claim":
        def changed(parent, conversation):
            original(parent, conversation)
            return ["different-id"]
        monkeypatch.setattr(receipts, "claim_wake", changed)
    else:
        monkeypatch.setattr(receipts, "finish_wake", lambda *_args: False)
    with pytest.raises(WorkerFailed):
        await wake(armed, receipts)
    entry = next(iter(work.bridge(armed).inputs.values()))
    await entry.producer.observer
    if fault == "finish":
        await entry.observer
        assert entry.state == "settled" and entry.receipt["outcome"] == "failed"
        assert entry.receipt["proof"]["worker_retired"] and not entry.committed_finished
    else:
        assert entry.state == "uncertain" and entry.receipt is None
    assert work.bridge(armed).error
    assert receipts.uncertain_wakes(PARENT, armed.convo_id) == [events[0]["completion_id"]]
    assert len(armed._rewrite_test_requests) == (1 if fault == "finish" else 0)
    assert await wake(armed, receipts) == []
    assert armed.conversation.count(receipt_message(events[0])) == 1


async def test_quit_cancels_claimed_wake_without_finishing_or_replaying_receipt(armed, tmp_path, monkeypatch):
    receipts, events = saved(armed, tmp_path, 1)
    entered = asyncio.Event()
    async def held(_origin):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(armed, "_stream_worker", held)
    worker = armed.run_worker(wake_parent(armed, parent=PARENT, receipts=receipts), group="child-wake", exit_on_error=False)
    await asyncio.wait_for(entered.wait(), 2)
    armed._gui_quitting = True
    work.begin_close(armed)
    proof = await asyncio.wait_for(work.settle_before_shutdown(armed), 5)
    armed._work_test_shutdown = proof
    assert proof["worker_settled"] and proof["callback_settled"] and proof["process_settled"]
    assert worker.is_finished and receipts.uncertain_wakes(PARENT, armed.convo_id) == [events[0]["completion_id"]]
    assert await wake(armed, receipts) == []
    assert armed.conversation.count(receipt_message(events[0])) == 1


async def test_prior_context_edit_is_preserved_and_never_reintroduces_committed_text(armed, tmp_path):
    receipts, events = saved(armed, tmp_path, 1)
    armed.conversation = [{"role":"system", "content":"retained system"},
                          {"role":"user", "content":"operator intentionally replaced context"}]
    armed._snapshot("intentional controlled context replacement")
    before = copy.deepcopy(armed.conversation)
    assert await wake(armed, receipts) == [events[0]["completion_id"]]
    await settle_inputs(armed, 1)
    assert armed.conversation[:-1] == before
    assert receipt_message(events[0]) not in armed.conversation
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.payload["history"]["context_sha256"] == work.digest(work.encoded(before))


async def test_duplicate_raw_receipt_proof_is_refused_before_claim(armed, tmp_path):
    receipts, events = saved(armed, tmp_path, 1)
    message = receipt_message(events[0])
    armed.store.write_record({"type":"msg", "completion_id":events[0]["completion_id"], "message":message})
    armed.conversation.append(message)
    with pytest.raises(ValueError, match="duplicated or changed"):
        _committed_wake(armed, parent=PARENT, receipts=receipts)
    assert receipts.pending_wakes(PARENT, armed.convo_id) == events
    assert not receipts.uncertain_wakes(PARENT, armed.convo_id) and not work.bridge(armed).inputs


@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "before-activation"])
async def test_startup_handshake_defers_committed_receipts_without_claim_then_wakes_after_ack(app, tmp_path, monkeypatch, captured):
    from dataclasses import replace

    from test_rust_background_input import command

    app._launch_options = replace(app._launch_options, work_lifecycle_version=1)
    monkeypatch.setattr(app, "_rpc_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(app, "_chat_running", lambda: any(w.group == "chat" and not w.is_finished for w in app.workers))
    receipts, events = saved(app, tmp_path, 1)
    descriptor = work.capture(app, command(app)) if captured else None
    before = app.convo_path.read_bytes()
    assert work.activation_pending(app)
    assert await wake(app, receipts) == []
    assert app.convo_path.read_bytes() == before
    assert receipts.pending_wakes(PARENT, app.convo_id) == events
    assert not receipts.uncertain_wakes(PARENT, app.convo_id)
    assert not app._rewrite_test_requests
    if descriptor is None:
        descriptor = work.capture(app, command(app))
    work.activate(app, command(app, instance=descriptor["instance"], activation_token=descriptor["activation_token"]))
    assert not work.activation_pending(app)
    assert await wake(app, receipts) == [events[0]["completion_id"]]
    await settle_inputs(app, 1)
    assert len(app._rewrite_test_requests) == 1
    assert app.conversation.count(receipt_message(events[0])) == 1
    assert not receipts.pending_wakes(PARENT, app.convo_id)
