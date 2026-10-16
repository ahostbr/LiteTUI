"""Scheduling ownership for selected generic producers, not universal provenance."""
from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from test_rust_mounted_runtime import MountedRuntime

from litetui import gui_rpc, hook_host
from litetui.app import LiteTUI
from litetui.event_origin import EventOrigin

pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]
OWNED_TYPES = {"turn_start", "text_delta", "reasoning_delta", "turn_end"}


async def test_generic_output_keeps_execution_a_across_queued_admission_b(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    original_create = runtime.create

    async def with_reasoning(transport, **kwargs):
        response = await original_create(transport, **kwargs)
        if kwargs.get("purpose", "turn") == "turn":
            response.delta.reasoning_content = "Fixture reasoning"
        return response

    monkeypatch.setattr(runtime, "create", with_reasoning)
    async with runtime.open() as (app, _pilot):
        request = await runtime.start_tool_turn()
        initiating_hook = next(doc["turn_id"] for doc in runtime.hook_documents
                               if doc["event"] == "prompt_before")
        await gui_rpc.async_dispatch(app, {"type": "gui.prompt.submit", "id": "operation-B",
                                          "message": "Queued prompt", "behavior": "queue"})
        await gui_rpc.async_dispatch(app, {"type": "gui.host_tools.result", "id": "result",
            "request_id": request["request_id"], "result": "Fixture tool result"})
        await runtime.wait(runtime.ended)
        await asyncio.wait_for(app.workers.wait_for_complete(), 10)
        envelopes = [row for row in runtime.records if row.get("type") == "gui.event"]
        owned = [row for row in envelopes if row["event"]["type"] in OWNED_TYPES]
        assert {row["event"]["type"] for row in owned} == OWNED_TYPES
        for index, row in enumerate(runtime.records):
            if row in owned:
                assert runtime.records[index - 1] == row["event"]
                assert "execution_id" not in row["event"]
                assert "origin" not in row["event"]
        executions = {row["execution_id"] for row in owned}
        assert len(executions) == 1
        assert UUID(executions.pop()).version == 4
        assert all(row["operation_id"] == "operation-A" for row in owned)
        assert all(row["hook_id"] == initiating_hook for row in owned)
        assert all(row["session_id"] == app.convo_id for row in owned)
        admissions = [doc for doc in runtime.hook_documents if doc["event"] == "prompt_before"]
        assert len(admissions) == 2 and admissions[1]["turn_id"] != initiating_hook
        assert app._hook_turn_id == admissions[1]["turn_id"]
        # Only explicit generic executor observations also participate; legacy
        # tools, native events and usage still have no inferred execution owner.
        attributed = OWNED_TYPES | {"generic_tool_requested", "generic_tool_returned"}
        assert all("execution_id" not in row for row in envelopes
                   if row["event"]["type"] not in attributed)


async def test_origin_is_immutable_normalized_and_each_schedule_is_fresh():
    from dataclasses import FrozenInstanceError

    mutable = {"id": ["bad"]}
    origin = EventOrigin(session_id=mutable, operation_id=mutable, hook_id=mutable,
                         source=mutable, execution_id=mutable)
    mutable["id"].append("changed")
    assert set(origin.envelope_fields().values()) == {None}
    with pytest.raises(FrozenInstanceError):
        origin.source = "changed"
    for invalid in (True, False, 1.5, [], {}):
        assert EventOrigin(operation_id=invalid).operation_id is None
    for valid in (0, 123, "operation"):
        assert EventOrigin(operation_id=valid).operation_id == valid
    seed = EventOrigin(session_id="session", operation_id=0, hook_id="hook",
                       source="typed", execution_id="old")
    a, b = seed.scheduled(), seed.scheduled()
    assert a.execution_id != b.execution_id != seed.execution_id
    assert UUID(a.execution_id).version == UUID(b.execution_id).version == 4
    fields = a.envelope_fields()
    fields["hook_id"] = "mutated"
    assert a.hook_id == "hook" and a.operation_id == 0


@pytest.mark.parametrize("inline", [False, True])
async def test_entry_captures_before_body_runs_without_borrowing_input_state(inline):
    from types import SimpleNamespace

    scheduled, started = [], []
    sentinel = object()

    async def body(origin):
        started.append(origin)
        return origin

    def run_worker(work, **options):
        scheduled.append((work, options))
        return sentinel

    app = SimpleNamespace(convo_id="session-A", _gui_operation_id="stale-operation",
                          _hook_turn_id="stale-hook", _hook_source="stale-source",
                          _stream_worker=body, run_worker=run_worker)
    entry = LiteTUI._stream_inline if inline else LiteTUI._stream
    result = entry(app)
    assert started == []
    app.convo_id = "session-B"
    if inline:
        assert scheduled == []
        captured = await result
    else:
        assert result is sentinel
        work, options = scheduled.pop()
        assert options == {"name": "_stream", "group": "chat", "exclusive": True,
                           "exit_on_error": True, "thread": False, "description": "_stream()"}
        captured = await work()
    assert captured.session_id == "session-A"
    assert captured.operation_id is captured.hook_id is captured.source is None
    assert UUID(captured.execution_id).version == 4
    # Reusing the same accepted origin is a NEW invocation, never token reuse.
    result = entry(app, origin=captured)
    again = await result if inline else await scheduled.pop()[0]()
    assert again.session_id == "session-A"
    assert again.execution_id != captured.execution_id


async def test_deferred_entry_has_no_turn_output_or_provider_request(tmp_path, monkeypatch):
    from litetui.turn_deferral import STREAM_DEFERRED, TurnDeferred

    runtime = MountedRuntime(tmp_path, monkeypatch)
    async with runtime.open() as (app, _pilot):
        async def defer():
            raise TurnDeferred("fixture maintenance")
        monkeypatch.setattr(app, "_await_mcp_maintenance", defer)
        before = len(runtime.records)
        worker = app._stream()
        assert worker.name == "_stream" and worker.group == "chat"
        assert await worker.wait() is STREAM_DEFERRED
        assert runtime.requests == []
        assert not any(row.get("type") in OWNED_TYPES for row in runtime.records[before:])


@pytest.mark.parametrize("hold_after_accept", [False, True])
async def test_admission_origin_survives_lifecycle_await_and_session_capture(
        tmp_path, monkeypatch, hold_after_accept):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    async with runtime.open() as (app, _pilot):
        original_session = app.convo_id
        item = {"content": "admitted", "text": "admitted", "operation_id": "A", "source": "typed"}
        entered, release = asyncio.Event(), asyncio.Event()
        original_drain = hook_host.drain_lifecycle

        async def held_drain(host):
            accepted = any(row.get("content") == "admitted" for row in host.conversation)
            if accepted == hold_after_accept:
                entered.set()
                await release.wait()
            await original_drain(host)

        monkeypatch.setattr(hook_host, "drain_lifecycle", held_drain)
        admission = asyncio.create_task(hook_host.admit_prompt_origin(app, item))
        await runtime.wait(entered)
        item["operation_id"] = "B"
        app._gui_operation_id = "B"
        app._hook_turn_id = "later-hook"
        app._hook_source = "later-source"
        release.set()
        captured = await admission
        initiating_hook = next(doc["turn_id"] for doc in runtime.hook_documents
                               if doc["event"] == "prompt_before")
        assert captured == EventOrigin(session_id=original_session, operation_id="A",
                                       hook_id=initiating_hook, source="typed")
        assert captured.execution_id is None
        assert runtime.requests == []


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_real_entries_propagate_origin_through_success_and_error(
        tmp_path, monkeypatch, inline, failure):
    from types import SimpleNamespace as NS

    from test_rust_mounted_runtime import ScriptedStream

    runtime = MountedRuntime(tmp_path, monkeypatch)
    original_create = runtime.create

    async def answer(transport, *, purpose="turn", **request):
        if purpose != "turn":
            return await original_create(transport, purpose=purpose, **request)
        runtime.requests.append({"purpose": purpose, **request})
        if failure:
            raise RuntimeError("fixture transport failure")
        stream = ScriptedStream(NS(content="Fixture answer", reasoning_content="Fixture reasoning",
                                   tool_calls=None), "stop")
        runtime.streams.append(stream)
        return stream

    monkeypatch.setattr(runtime, "create", answer)
    async with runtime.open() as (app, _pilot):
        await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "hello", "protocol_version": 1})
        accepted = hook_host.accept_prompt_origin(app, {
            "content": "Fixture prompt", "source": "typed", "operation_id": "A"})
        assert accepted is not None
        # Run actual inline execution with accepted input; direct calls must NOT
        # attribute themselves to these same ambient admission fields.
        before = len(runtime.records)
        if inline:
            await app._stream_inline(origin=accepted)
        else:
            worker = app._stream()
            assert worker.name == "_stream" and worker.group == "chat"
            await worker.wait()
        owned = [row for row in runtime.records[before:] if row.get("type") == "gui.event"
                 and row["event"]["type"] in OWNED_TYPES]
        assert owned[0]["event"]["type"] == "turn_start"
        assert owned[-1]["event"]["stopReason"] == ("error" if failure else "stop")
        assert len({row["execution_id"] for row in owned}) == 1
        assert UUID(owned[0]["execution_id"]).version == 4
        for row in owned:
            assert row["operation_id"] == ("A" if inline else None)
            assert row["hook_id"] == (accepted.hook_id if inline else None)
            assert row["source"] == ("typed" if inline else None)
            assert row["session_id"] == accepted.session_id
            assert "origin" not in row["event"] and "execution_id" not in row["event"]
        if not failure:
            assert {row["event"]["type"] for row in owned} == OWNED_TYPES
