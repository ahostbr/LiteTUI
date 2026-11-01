"""Post-compaction wake uses exact captured admission before a user append."""
import asyncio
from dataclasses import replace

import pytest
from test_rust_background_input import app as _owned_app
from test_rust_background_input import armed as _armed
from test_rust_background_input import command, settle_inputs
from test_rust_background_input import switch_app as _switch_app

from litetui import background_input as work
from litetui import hook_host
from litetui.app import WAKE_AFTER_COMPACT

app = _owned_app
armed = _armed
switch_app = _switch_app

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("captured", [False, True], ids=["before-capture", "before-activation"])
async def test_startup_compact_wake_does_not_materialise_append_or_start(app, monkeypatch, captured):
    app._launch_options = replace(app._launch_options, work_lifecycle_version=1)
    if captured:
        work.capture(app, command(app))
    effects = []
    for name in ("_materialise_convo", "_user_bubble", "_append", "_stream"):
        monkeypatch.setattr(app, name, lambda *_args, _name=name, **_kwargs: effects.append(_name))
    before = app.convo_path.read_bytes()
    app._wake_after_compact()
    assert effects == []
    assert app.convo_path.read_bytes() == before
    assert not app._pending_input


async def test_active_compact_wake_has_exact_payload_one_append_and_suppressed_hooks(armed, monkeypatch):
    original = armed._stream_worker
    observed = []
    async def stream(origin):
        observed.append((origin, armed._hooks_suppressed, armed._active_tool_profile))
        await original(origin)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("canonical compaction wake must suppress prompt hooks")
    monkeypatch.setattr(armed, "_stream_worker", stream)
    monkeypatch.setattr(hook_host, "snapshot", forbidden)
    before = armed.convo_path.read_bytes()
    armed._wake_after_compact()
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["kind"] == "compact_wake"
    assert entry.payload["content"] == WAKE_AFTER_COMPACT
    assert entry.ref["source"] == "compact-wake"
    assert entry.ref["requested_profile"] == "strict"
    assert entry.admission["mode"] == "new_turn"
    assert entry.admission["origin"]["source"] == "compact-wake"
    assert entry.state == "settled" and entry.receipt["outcome"] == "completed"
    assert entry.producer.phase == "retired" and entry.producer.worker.is_finished
    rows = [row for row in armed.conversation if row.get("content") == WAKE_AFTER_COMPACT]
    assert len(rows) == 1 and rows[0]["work_input_id"] == entry.ref["input_id"]
    assert len(observed) == 1 and observed[0][1:] == (True, "strict")
    assert armed.convo_path.read_bytes() != before and not armed._pending_input
    emitted = [event for event, _origin in armed._work_test_events]
    assert next(i for i,e in enumerate(emitted) if e["type"] == "work_admission") < next(i for i,e in enumerate(emitted) if e["type"] == "work_settled")


@pytest.mark.parametrize("reason", ["busy", "queued", "abandoned", "stopped", "quit", "closing", "uncertain", "recapture", "readback"])
async def test_compact_wake_guard_preserves_canonical_history_and_creates_no_work(armed, monkeypatch, reason):
    if reason == "busy":
        monkeypatch.setattr(armed, "_chat_running", lambda: True)
    elif reason == "queued":
        armed._pending_input.append({"content":"real user input", "source":"typed", "tool_profile":"strict"})
    elif reason == "abandoned":
        armed._turn_abandoned = True
    elif reason == "stopped":
        armed._stop_requested = True
    elif reason == "quit":
        armed._gui_quitting = True
    elif reason == "closing":
        work.begin_close(armed)
    elif reason == "recapture":
        armed._native_work_recapture_required = {"previous_instance": "9"*64, "identity": work.identity(armed)}
    elif reason == "readback":
        work.bridge(armed).pending_readbacks["selected-input"] = {"receipt":"unconfirmed"}
    else:
        work.bridge(armed).error = "Owned uncertain work requires reconciliation"
    before = armed.convo_path.read_bytes()
    armed._wake_after_compact()
    await asyncio.sleep(0)
    assert armed.convo_path.read_bytes() == before
    assert not work.bridge(armed).inputs and not work.bridge(armed).producers
    assert not armed._rewrite_test_requests


@pytest.mark.parametrize("reason", ["busy", "queued", "abandoned", "stopped", "quit"])
async def test_real_turn_or_stop_wins_delivery_worker_schedule_gap(armed, monkeypatch, reason):
    before = armed.convo_path.read_bytes()
    armed._wake_after_compact()
    if reason == "busy":
        monkeypatch.setattr(armed, "_chat_running", lambda: True)
    elif reason == "queued":
        armed._pending_input.append({"content":"winning input", "source":"typed", "tool_profile":"strict"})
    elif reason == "abandoned":
        armed._turn_abandoned = True
    elif reason == "stopped":
        armed._stop_requested = True
    else:
        armed._gui_quitting = True
    producers = list(work.bridge(armed).producers.values())
    assert len(producers) == 1
    await asyncio.wait_for(producers[0].observer, 3)
    assert producers[0].phase == "retired" and producers[0].worker.is_finished
    assert not work.bridge(armed).inputs
    assert armed.convo_path.read_bytes() == before and not armed._rewrite_test_requests


async def test_compact_wake_aftercare_retains_exact_action_predecessor_and_hook_suppression(armed, monkeypatch):
    from test_rust_conversation_rewrite import apply, reviewed, settle

    from litetui import conversation_action as action
    original = armed._stream_worker
    calls = []
    async def stream(origin):
        calls.append((origin, armed._hooks_suppressed))
        action.segment(armed, origin)
        await original(origin)
        if len(calls) == 1:
            action.after_refresh(armed, armed._wake_after_compact)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    review = reviewed(armed)
    assert (await apply(armed, review))["turn"] == "accepted"
    proof = await asyncio.wait_for(settle(armed, review), 5)
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["kind"] == "compact_wake"
    assert entry.ref["source"] == "compact-wake"
    assert entry.admission["mode"] == "action_segment"
    assert entry.admission["operation_id"] == "rewrite-execution"
    assert entry.admission["parent_execution_id"] == calls[0][0].execution_id
    assert entry.admission["origin"]["source"] == "rpc"
    assert len(calls) == 2 and calls[1][1] is True
    assert proof["execution"]["segments"] == 2 and not action._attempt(armed).pending


async def test_quit_after_wake_admission_joins_exact_stream_and_retains_one_input(armed, monkeypatch):
    entered = asyncio.Event()
    async def held(_origin):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(armed, "_stream_worker", held)
    armed._wake_after_compact()
    await asyncio.wait_for(entered.wait(), 2)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.admission is not None and entry.worker is not None
    armed._gui_quitting = True
    work.begin_close(armed)
    proof = await asyncio.wait_for(work.settle_before_shutdown(armed), 5)
    armed._work_test_shutdown = proof
    assert proof["worker_settled"] and proof["callback_settled"] and proof["process_settled"]
    assert entry.worker.is_finished and entry.producer.worker.is_finished
    assert entry.receipt["outcome"] == "cancelled" and entry.state == "settled"
    assert len([row for row in armed.conversation if row.get("content") == WAKE_AFTER_COMPACT]) == 1
    armed._wake_after_compact()
    assert len(work.bridge(armed).inputs) == 1 and not armed._pending_input


async def test_first_manual_compact_wake_binds_finished_maintenance_root_without_a_prior_turn(armed, monkeypatch):
    import copy

    from test_rust_conversation_compact import apply, fake_root, reviewed, settled

    from litetui import conversation_action as action
    from litetui import conversation_compact as compact

    expected = [copy.deepcopy(armed.conversation[0]), {"role":"user", "content":"Controlled compact summary"}]
    async def publish(self):
        publication = compact.begin_publication(self, expected)
        record = self.store.truncate_record(len(self.conversation), expected[1:], True, "controlled compact root")
        compact.write_publication_record(self, record)
        self.conversation = copy.deepcopy(expected)
        compact.verify_publication(self, publication)
        action.after_refresh(self, self._wake_after_compact)
        return compact.CompactOutcome("compacted", "compacted", publication_attempted=True, publication_verified=True)
    fake_root(armed, monkeypatch, publish)
    original = armed._stream_worker
    async def stream(origin):
        action.segment(armed, origin)
        await original(origin)
    monkeypatch.setattr(armed, "_stream_worker", stream)
    review = reviewed(armed)
    assert (await apply(armed, review))["status"] == "accepted"
    proof = await asyncio.wait_for(settled(armed, review), 5)
    await settle_inputs(armed, 1)
    entry = next(iter(work.bridge(armed).inputs.values()))
    assert entry.producer.ref["causal_action_id"] == review["action_id"]
    assert entry.producer.ref["causal_operation_id"] == "compact-operation"
    assert entry.producer.ref["causal_execution_id"] == review["root_execution_id"]
    assert entry.admission["mode"] == "action_segment"
    assert entry.admission["parent_execution_id"] == review["root_execution_id"]
    assert entry.admission["origin"]["operation_id"] == "compact-operation"
    assert proof["execution"]["segments"] == 1 and proof["outcome"] == "compacted"
    assert entry.state == "settled" and entry.producer.worker.is_finished


@pytest.mark.parametrize("fault", ["unfinished", "wrong_execution", "unregistered"])
async def test_manual_compact_root_cannot_be_replaced_or_claimed_before_retirement(armed, monkeypatch, fault):
    import copy
    from types import SimpleNamespace

    from test_rust_conversation_compact import apply, fake_root, reviewed, settled

    from litetui import conversation_action as action
    from litetui import conversation_compact as compact

    observed = []
    def _wake_after_compact():
        attempt = action._required_attempt(armed)
        execution, started = attempt.execution, attempt.root_started
        try:
            if fault == "unfinished":
                attempt.root_started = False
            elif fault == "wrong_execution":
                attempt.execution = replace(execution, origin=replace(execution.origin, execution_id=work.ident()))
            else:
                attempt.execution = replace(execution, worker=SimpleNamespace(is_finished=True))
            with pytest.raises(ValueError, match="exact retired maintenance root"):
                work.start_compact_wake(armed, WAKE_AFTER_COMPACT)
            observed.append(True)
            assert not work.bridge(armed).producers and not work.bridge(armed).reservations
        finally:
            attempt.execution, attempt.root_started = execution, started
    async def publish(self):
        expected = [copy.deepcopy(self.conversation[0]), {"role":"user", "content":"Verified compact summary"}]
        publication = compact.begin_publication(self, expected)
        compact.write_publication_record(self, self.store.truncate_record(len(self.conversation), expected[1:], True, "controlled root"))
        self.conversation = expected
        compact.verify_publication(self, publication)
        action.after_refresh(self, _wake_after_compact)
        return compact.CompactOutcome("compacted", "compacted", publication_attempted=True, publication_verified=True)
    fake_root(armed, monkeypatch, publish)
    review = reviewed(armed)
    assert (await apply(armed, review))["status"] == "accepted"
    proof = await asyncio.wait_for(settled(armed, review), 5)
    assert observed == [True] and proof["execution"]["segments"] == 0
    assert not work.bridge(armed).inputs and not armed._rewrite_test_requests
