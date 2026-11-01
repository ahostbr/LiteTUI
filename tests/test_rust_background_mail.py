"""Captured mail is durable before claim and has one source-owned execution."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import settle_inputs
from test_rust_background_input import switch_app as _switch_app

from litetui import background_input as work
from litetui import background_mail as mail
from litetui import harness

app = _owned_app
armed = _armed
switch_app = _switch_app


def mailbox(app, tmp_path, monkeypatch, body="Captured inbox message"):
    new, done = tmp_path / "mail-new", tmp_path / "mail-done"
    new.mkdir()
    done.mkdir()
    monkeypatch.setattr(harness, "NEW", new)
    monkeypatch.setattr(harness, "DONE", done)
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    if hasattr(app, "_loop"):
        # Existing source fixture is deliberately unmounted. Supply the actual
        # test loop/thread to exercise Textual's real cross-thread dispatcher.
        monkeypatch.setattr(app, "_loop", asyncio.get_running_loop())
        monkeypatch.setattr(app, "_thread_id", threading.get_ident())
    app.seat = harness.Seat("test-mail-seat", "Controlled", "fixture")
    value = {"from": "controlled-sender", "to": app.seat.agent_id, "priority": "normal", "body": body}
    path = new / "exact-mail.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, done, value


async def producers_retired(app):
    await asyncio.wait_for(asyncio.gather(*(p.observer for p in work.bridge(app).producers.values() if p.observer)), 5)


@pytest.mark.asyncio
async def test_mail_is_durable_before_external_claim_then_admitted_once(armed, tmp_path, monkeypatch):
    path, done, message = mailbox(armed, tmp_path, monkeypatch)
    actual_move = harness._move
    inspected = []

    def move(source, destination):
        document = json.loads((armed.convo_dir / work.FILENAME).read_text(encoding="utf-8"))
        entry = document["inputs"][0]
        assert entry["state"] == "queued" and entry["admission"] is None
        assert entry["payload"]["text"] == harness.format_message(message)
        assert armed._rewrite_test_requests == []
        inspected.append(source.name)
        return actual_move(source, destination)

    monkeypatch.setattr(harness, "_move", move)
    assert await mail.poll(armed) == []
    await settle_inputs(armed, 1)
    await producers_retired(armed)
    assert inspected == [path.name] and not path.exists() and len(list(done.glob("*.json"))) == 1
    assert len(armed._rewrite_test_requests) == 1
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["kind"] == "inbox" and entry.state == "settled"
    assert entry.admission["origin"]["source"] == "harness"
    assert len([m for m in armed.conversation if m.get("work_input_id") == entry.ref["input_id"]]) == 1


@pytest.mark.asyncio
async def test_failed_mail_move_retains_exact_input_and_never_wakes_or_replays(armed, tmp_path, monkeypatch):
    path, _, message = mailbox(armed, tmp_path, monkeypatch)
    monkeypatch.setattr(harness, "_move", lambda *_args: None)
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    assert await mail.poll(armed) == []
    await producers_retired(armed)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert path.exists() and entry.state == "held" and entry.admission is None
    assert entry.payload["text"] == harness.format_message(message)
    assert await mail.poll(armed) == []
    assert len(work.bridge(armed).inputs) == 1 and armed._rewrite_test_requests == []
    assert any("automatic replay refused" in notice for notice in notices)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["oversize", "capacity", "write"])
async def test_mail_refusal_leaves_original_unclaimed(armed, tmp_path, monkeypatch, fault):
    path, done, _ = mailbox(armed, tmp_path, monkeypatch, "x" * 70000 if fault == "oversize" else "Keep this mail")
    before = path.read_bytes()
    notices: list[str] = []
    monkeypatch.setattr(armed, "_system", notices.append)
    if fault == "capacity":
        def full(_app):
            raise ValueError("Work completion capacity is full")
        monkeypatch.setattr(work, "reserve", full)
    if fault == "write":
        def failed():
            raise OSError("Controlled durable intake failure")
        monkeypatch.setattr(work.bridge(armed), "persist", failed)
    assert await mail.poll(armed) == []
    assert path.read_bytes() == before and not list(done.iterdir())
    assert not armed._rewrite_test_requests and notices


@pytest.mark.asyncio
async def test_quit_between_intake_and_claim_retires_worker_and_holds_input(armed):
    message = {"from": "controlled", "body": "Keep during Quit", "priority": "normal"}
    intake = await mail.prepare(armed, message, "exact-preclaim")
    assert intake.entry.state == "queued" and not armed._pending_input
    work.begin_close(armed)
    proof = await work.settle_before_shutdown(armed)
    assert proof["worker_settled"] and intake.worker.is_finished
    assert intake.entry.state == "held" and intake.entry.admission is None
    assert mail.finish(intake, True)
    await asyncio.sleep(0)
    assert not armed._rewrite_test_requests
    armed._work_test_shutdown = proof


@pytest.mark.asyncio
async def test_direct_mail_routes_through_actual_source_worker(armed):
    message = {"from": "controlled", "body": "Direct delivered mail", "priority": "normal"}
    armed._deliver_inbox(message)
    await settle_inputs(armed, 1)
    await producers_retired(armed)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["kind"] == "inbox"
    assert entry.payload["text"] == harness.format_message(message)
    assert len(armed._rewrite_test_requests) == 1


def test_poll_prepare_is_opt_in_and_failed_claim_is_reported(tmp_path, monkeypatch):
    class App:
        seat: harness.Seat
    app = App()
    path, _, _ = mailbox(app, tmp_path, monkeypatch)
    assert len(app.seat.poll()) == 1 and not path.exists()
