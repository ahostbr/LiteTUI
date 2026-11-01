"""Captured mailbox claims retain input and join their exact external thread."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from test_rust_background_input import command
from test_rust_background_mail import app as _owned_app
from test_rust_background_mail import armed as _armed
from test_rust_background_mail import mailbox, producers_retired
from test_rust_background_mail import switch_app as _switch_app

from litetui import background_input as work
from litetui import background_mail as mail
from litetui import harness

app = _owned_app
armed = _armed
switch_app = _switch_app


async def _thread_reached(event: threading.Event) -> None:
    async def reached() -> None:
        while not event.is_set():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(reached(), 5)


@pytest.mark.asyncio
async def test_recovered_held_mail_is_not_captured_or_replayed_again(armed, tmp_path, monkeypatch):
    path, done, message = mailbox(armed, tmp_path, monkeypatch)
    original_bytes = path.read_bytes()
    monkeypatch.setattr(harness, "_move", lambda *_args: None)
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    assert await mail.poll(armed) == []
    await producers_retired(armed)
    original = next(iter(work.bridge(armed).inputs.values()))
    assert original.state == "held" and original.admission is None
    work.begin_close(armed)
    await work.settle_before_shutdown(armed)

    # Recover the actual durable ledger through a fresh capture/activation,
    # after the previous epoch's producer and thread have independently retired.
    armed._native_work_bridge = None
    captured = work.capture(armed, command(armed))
    work.activate(armed, command(armed, instance=captured["instance"],
                                 activation_token=captured["activation_token"]))
    value = work.bridge(armed)
    assert len(captured["recovered"]) == 1 and not value.producers
    assert await mail.poll(armed) == []
    await producers_retired(armed)

    assert list(value.inputs) == [original.ref["input_id"]]
    assert not value.producers
    assert path.read_bytes() == original_bytes and not list(done.iterdir())
    assert value.inputs[original.ref["input_id"]].payload["text"] == harness.format_message(message)
    assert not armed._rewrite_test_requests
    assert any("automatic replay refused" in notice for notice in notices)


@pytest.mark.asyncio
async def test_cancelled_poll_and_shutdown_join_actual_claim_thread(armed, tmp_path, monkeypatch):
    path, done, _ = mailbox(armed, tmp_path, monkeypatch)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    actual_move, actual_poll = harness._move, armed.seat.poll

    def move(source, destination):
        started.set()
        assert release.wait(10), "controlled claim was not released"
        return actual_move(source, destination)

    def poll(**kwargs):
        try:
            return actual_poll(**kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(harness, "_move", move)
    monkeypatch.setattr(armed.seat, "poll", poll)
    polling = asyncio.create_task(mail.poll(armed))
    closing = None
    escaped = None
    try:
        await _thread_reached(started)
        polling.cancel()
        work.begin_close(armed)
        closing = asyncio.create_task(work.settle_before_shutdown(armed))
        await asyncio.sleep(0.05)
        escaped = (polling.done(), closing.done(), finished.is_set(), path.exists())
    finally:
        # Always retire the real thread, including when a regression is observed.
        release.set()
        await _thread_reached(finished)
        result = await asyncio.wait_for(asyncio.gather(polling, return_exceptions=True), 5)
        if closing is not None:
            proof = await asyncio.wait_for(asyncio.shield(closing), 5)
            armed._work_test_shutdown = proof

    assert escaped == (False, False, False, True), escaped
    assert isinstance(result[0], asyncio.CancelledError)
    assert proof["worker_settled"] and finished.is_set()
    assert not path.exists() and len(list(done.glob("*.json"))) == 1
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.state == "held" and entry.admission is None
    assert not armed._rewrite_test_requests and not armed._pending_input


@pytest.mark.asyncio
async def test_oversized_raw_metadata_is_left_unclaimed_without_producer(armed, tmp_path, monkeypatch):
    path, done, message = mailbox(armed, tmp_path, monkeypatch, "Small visible message")
    # Unknown metadata must not bypass the complete raw-message byte bound.
    message["unrendered_metadata"] = "x" * (mail.RAW_MAIL_BYTES + 1)
    path.write_text(json.dumps(message), encoding="utf-8")
    original = path.read_bytes()
    assert await mail.poll(armed) == []
    assert path.read_bytes() == original and not list(done.iterdir())
    assert not work.bridge(armed).inputs and not work.bridge(armed).producers
    assert not armed._rewrite_test_requests


@pytest.mark.asyncio
async def test_replaced_mail_claim_never_delivers_original_or_foreign_bytes(armed, tmp_path, monkeypatch):
    path, done, message = mailbox(armed, tmp_path, monkeypatch, "Original durable message")
    actual_move = harness._move
    replacement = {**message, "to": "another-controlled-seat", "body": "Replacement never authorized"}
    replacement_bytes = json.dumps(replacement).encode("utf-8")

    def move(source, destination):
        # The first read and durable preparation have completed. Replacing just
        # before the actual claim exercises post-move identity/content readback.
        source.write_bytes(replacement_bytes)
        return actual_move(source, destination)

    monkeypatch.setattr(harness, "_move", move)
    assert await mail.poll(armed) == []
    await producers_retired(armed)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.payload["text"] == harness.format_message(message)
    assert entry.state == "held" and entry.admission is None
    assert not armed._rewrite_test_requests and not armed._pending_input
    assert not path.exists() and (done / path.name).read_bytes() == replacement_bytes


def _pending_control(app, message, *, relay):
    from litetui import approval_authority, approval_relay, tool_approval

    ident = "appr-0123456789ab"
    parent = message["from"]
    harness.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    (harness.AGENTS_DIR / (parent + ".json")).write_text(
        json.dumps({"agent_id": parent}), encoding="utf-8")
    future = asyncio.get_running_loop().create_future()
    approval_authority.create(app, ident, approver=parent, route="spawner", timeout=120)
    if relay:
        approval_relay._pending(app)[ident] = (future, parent)
        assert approval_relay.pending_answer(app, message) is not None
    else:
        tool_approval._approval_registry(app)[ident] = future
        assert tool_approval.pending_spawner_answer(app, message) is not None
    return ident, future


@pytest.mark.asyncio
@pytest.mark.parametrize("relay", [False, True])
async def test_expired_control_during_claim_never_becomes_ordinary_prompt(armed, tmp_path, monkeypatch, relay):
    from litetui import approval_authority

    path, done, message = mailbox(armed, tmp_path, monkeypatch, "APPROVE appr-0123456789ab")
    ident, future = _pending_control(armed, message, relay=relay)
    actual_move = harness._move
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)

    def expire():
        assert approval_authority.close(armed, ident, "timeout") == "timeout"
        future.cancel()

    def move(source, destination):
        # Both canonical eligibility matchers accepted the exact pending ID at
        # preparation. Its authority expires while the external claim runs.
        armed.call_from_thread(expire)
        return actual_move(source, destination)

    monkeypatch.setattr(harness, "_move", move)
    assert await mail.poll(armed) == []
    assert future.cancelled()
    assert not path.exists() and (done / path.name).is_file()
    assert not work.bridge(armed).inputs and not work.bridge(armed).producers
    assert not armed._rewrite_test_requests and not armed._pending_input
    assert any("control receipt" in notice for notice in notices)


@pytest.mark.asyncio
async def test_pending_control_resolves_while_execution_admission_is_blocked(armed, tmp_path, monkeypatch):
    from litetui import approval_authority, tool_approval

    path, done, message = mailbox(armed, tmp_path, monkeypatch, "APPROVE appr-0123456789ab")
    ident, future = _pending_control(armed, message, relay=False)
    refusals = []

    def execution_blocked():
        refusals.append("execution")
        raise ValueError("Controlled pending conversation action owns execution")

    monkeypatch.setattr(armed, "_validate_owned_execution", execution_blocked)
    with pytest.raises(ValueError, match="pending conversation action"):
        work.capture_tool(armed, lambda: None)
    assert await mail.poll(armed) == []
    assert refusals == ["execution"]
    assert future.done() and future.result() == tool_approval.ONCE
    assert not path.exists() and (done / path.name).is_file()
    assert not work.bridge(armed).inputs and not work.bridge(armed).producers
    assert not armed._rewrite_test_requests and not armed._pending_input
    assert approval_authority.close(armed, ident, "cancelled") == "approved"


@pytest.mark.asyncio
async def test_control_has_one_observer_slot_at_full_execution_callback_capacity(armed, tmp_path, monkeypatch):
    from litetui import approval_authority, tool_approval

    path, done, message = mailbox(armed, tmp_path, monkeypatch, "APPROVE appr-0123456789ab")
    ident, future = _pending_control(armed, message, relay=False)
    # Reserve the full real callback table before any callback starts; this is
    # also the admission window in which a producer can await permission.
    registered = [work.capture_tool(armed, lambda: None) for _ in range(work.LIMITS["producers"])]
    actual_poll = armed.seat.poll
    checked = []

    def check_observer_bound():
        value = work.bridge(armed)
        observers = [call for call in value.calls if call.observer and not call.retired.is_set()]
        assert len(observers) == 1
        with pytest.raises(ValueError, match="callback capacity"):
            work.capture_observer(armed, lambda: None)
        checked.append(True)

    def poll(**kwargs):
        armed.call_from_thread(check_observer_bound)
        return actual_poll(**kwargs)

    monkeypatch.setattr(armed.seat, "poll", poll)
    try:
        with pytest.raises(ValueError, match="callback capacity"):
            work.capture_tool(armed, lambda: None)
        assert await mail.poll(armed) == []
        assert checked == [True] and future.result() == tool_approval.ONCE
        assert not path.exists() and (done / path.name).is_file()
        assert not work.bridge(armed).inputs and not work.bridge(armed).producers
        assert not armed._rewrite_test_requests and not armed._pending_input
        # The one observer slot is released only after its actual thread retires.
        callback, observer = work.capture_observer(armed, lambda: None)
        await work.track_tool(observer, asyncio.to_thread(callback))
        await work.finish_tool(observer)
        assert observer.retired.is_set()
    finally:
        # Run/join the inert callbacks sequentially through the production
        # lifetime seam; never create 256 concurrent OS threads or providers.
        for callback, call in registered:
            await work.track_tool(call, asyncio.to_thread(callback))
            await work.finish_tool(call)
        approval_authority.close(armed, ident, "cancelled")
