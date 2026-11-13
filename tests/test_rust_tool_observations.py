"""Generic executor observations; never authorization or background completion."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS
from uuid import UUID

import pytest
from test_rust_mounted_runtime import MountedRuntime, ScriptedStream

from litetui import gui_rpc, hook_host, sanitize

pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]
KINDS = {"generic_tool_requested", "generic_tool_returned"}


def observations(runtime):
    return [row for row in runtime.records if row.get("type") == "gui.event"
            and row["event"]["type"] in KINDS]


async def test_real_host_return_keeps_origin_across_queued_prompt(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    async with runtime.open() as (app, _pilot):
        request = await runtime.start_tool_turn()
        start = next(row for row in runtime.records if row.get("type") == "gui.event"
                     and row["event"]["type"] == "turn_start")
        pending = observations(runtime)
        assert len(pending) == 1
        assert pending[0]["event"]["type"] == "generic_tool_requested"
        await gui_rpc.async_dispatch(app, {"type": "gui.prompt.submit", "id": "B",
                                          "message": "Queued B", "behavior": "queue"})
        await gui_rpc.async_dispatch(app, {"type": "gui.host_tools.result", "id": "result",
            "request_id": request["request_id"], "result": "Fixture tool result"})
        await runtime.wait(runtime.ended)
        await asyncio.wait_for(app.workers.wait_for_complete(), 10)
        rows = observations(runtime)
        assert len(rows) == 2
        requested, returned = [row["event"] for row in rows]
        assert requested == {"type": "generic_tool_requested",
            "observation_id": requested["observation_id"], "round": 0, "slot": 0,
            "provider_call_id": "fixture-tool-call", "name": "fixture_roundtrip",
            "arguments": requested["arguments"]}
        assert json.loads(requested["arguments"]) == {"value": "A"}
        assert UUID(requested["observation_id"]).version == 4
        assert returned == {"type": "generic_tool_returned",
            "observation_id": requested["observation_id"],
            "result": "Fixture tool result", "ok": True}
        for row in rows:
            for key in ("execution_id", "session_id", "operation_id", "hook_id", "source"):
                assert row[key] == start[key]
            assert runtime.records[runtime.records.index(row) - 1] == row["event"]
        # Preserve the legacy record, including its lack of invocation identity.
        legacy = [row for row in runtime.records if row.get("type") == "tool_call"]
        assert legacy == [{"type": "tool_call", "name": "fixture_roundtrip", "args": {"value": "A"}}]
        for row in runtime.records:
            if row.get("type") == "gui.event" and row["event"]["type"] in {
                    "tool_call", "host_tool_requested", "tool_approval_requested", "usage"}:
                assert "execution_id" not in row


def script_rounds(runtime, monkeypatch, rounds):
    original_create = runtime.create
    count = 0

    async def create(transport, *, purpose="turn", **request):
        nonlocal count
        if purpose != "turn":
            return await original_create(transport, purpose=purpose, **request)
        runtime.requests.append({"purpose": purpose, **request})
        if count < len(rounds):
            calls = [NS(index=i, id=call_id, type="function",
                        function=NS(name="fixture_roundtrip", arguments=args))
                     for i, (call_id, args) in enumerate(rounds[count])]
            delta = NS(content=None, reasoning_content=None, tool_calls=calls)
            finish = "tool_calls"
        else:
            delta = NS(content="Fixture answer", reasoning_content=None, tool_calls=None)
            finish = "stop"
        count += 1
        stream = ScriptedStream(delta, finish)
        runtime.streams.append(stream)
        return stream

    monkeypatch.setattr(runtime, "create", create)


async def run_inline(app):
    await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "hello", "protocol_version": 1})
    accepted = hook_host.accept_prompt_origin(app, {
        "content": "Fixture prompt", "source": "typed", "operation_id": "A"})
    assert accepted is not None
    try:
        await app._stream_inline(origin=accepted)
    except BaseException:
        # Injected failures bypass the normal tool/end cleanup. The existing
        # elapsed painter otherwise waits forever on the still-inflight tool.
        # Explicitly retire this test-owned clock; do not claim runtime error
        # cleanup coverage or weaken MountedRuntime's task-settlement checks.
        app._elapsed.cancel()
        raise


async def test_distinct_calls_carry_redacted_arguments_and_clean_return(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    secret = "fixture-secret-value-123456789"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    args_text = json.dumps({"nested": {"value": secret}, "api_key": secret, "background": True})
    # Absent IDs become call_0 again in round 1; provider IDs are not identities.
    script_rounds(runtime, monkeypatch, [[(None, args_text), ("repeated", "{}")],
                                       [(None, "{}"), ("repeated", "{}")]])
    received = []
    async with runtime.open() as (app, _pilot):
        async def execute(name, args):
            received.append((name, json.loads(json.dumps(args))))
            args.clear()  # The actual door may pop background and mutate args.
            return "\x1b[31mOPENAI_API_KEY=" + secret + "\x1b[0m", True
        monkeypatch.setattr(app, "_execute_tool", execute)
        await run_inline(app)
        events = [row["event"] for row in observations(runtime)]
        assert len(events) == 8
        ids = set()
        for n in range(4):
            requested, returned = events[n * 2:n * 2 + 2]
            ids.add(requested["observation_id"])
            assert UUID(requested["observation_id"]).version == 4
            assert requested["round"] == n // 2 and requested["slot"] == n % 2
            assert requested["provider_call_id"] == ("call_0" if n % 2 == 0 else "repeated")
            assert returned["observation_id"] == requested["observation_id"]
            assert returned["result"] == "OPENAI_API_KEY=" + sanitize.REDACTED_MARKER
            assert secret not in json.dumps(returned)
            # The raw argument string ToolMessage shows, with the result's hygiene: no
            # secret-NAMED value crosses (Sentinel 82b6ee9d changed the 6c146758 contract).
            assert requested["arguments"] == sanitize.redact_secrets(args_text if n == 0 else "{}")
            assert "\x1b" not in returned["result"]
        assert len(ids) == 4
        assert all("arguments_text" not in event and "args" not in event for event in events)
        first = json.loads(events[0]["arguments"])
        assert first["api_key"] == sanitize.REDACTED_MARKER
        # A value under an ordinary key is data to the redactor, as on Python's own screen and
        # in the legacy tool_call event's args.
        assert first["nested"]["value"] == secret
        assert received[0][1]["nested"]["value"] == secret  # execution unchanged
        assert received[0][1]["background"] is True
        assert len({row["execution_id"] for row in observations(runtime)}) == 1


@pytest.mark.parametrize("args_text", ["{broken", "[]"])
async def test_invalid_arguments_have_no_executor_observations(tmp_path, monkeypatch, args_text):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    script_rounds(runtime, monkeypatch, [[("call", args_text)]])
    async with runtime.open() as (app, _pilot):
        async def forbidden(*_args):
            pytest.fail("invalid arguments reached executor")
        monkeypatch.setattr(app, "_execute_tool", forbidden)
        await run_inline(app)
        assert observations(runtime) == []
        assert any(row.get("role") == "tool" and "invalid tool arguments" in row["content"]
                   for row in app.conversation)


async def test_policy_refusal_is_normal_return_not_dispatch(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    script_rounds(runtime, monkeypatch, [[("call", "{}")]])
    async with runtime.open() as (app, _pilot):
        app.tools_enabled = False
        await run_inline(app)
        events = [row["event"] for row in observations(runtime)]
        assert len(events) == 2
        assert events[1]["ok"] is False
        tool_result = next(row["content"] for row in app.conversation if row.get("role") == "tool")
        assert events[1]["result"] == tool_result
        assert not any(row.get("type") == "host_tool_requested" for row in runtime.records)


@pytest.mark.parametrize("outcome", ["exception", "cancel", "receipt"])
async def test_return_observation_does_not_fabricate_completion(tmp_path, monkeypatch, outcome):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    script_rounds(runtime, monkeypatch, [[("call", "{}")]])
    async with runtime.open() as (app, _pilot):
        async def execute(_name, _args):
            if outcome == "exception":
                raise RuntimeError("fixture escaped executor")
            if outcome == "cancel":
                raise asyncio.CancelledError("fixture cancelled executor")
            return "Background task started; not completed", True
        monkeypatch.setattr(app, "_execute_tool", execute)
        if outcome == "receipt":
            await run_inline(app)
        else:
            error = RuntimeError if outcome == "exception" else asyncio.CancelledError
            with pytest.raises(error):
                await run_inline(app)
        events = [row["event"] for row in observations(runtime)]
        assert events[0]["type"] == "generic_tool_requested"
        if outcome == "receipt":
            assert len(events) == 2
            assert events[1]["result"] == "Background task started; not completed"
            assert events[1]["ok"] is True
            assert "completed" not in events[1] and "dispatched" not in events[1]
        else:
            assert len(events) == 1


@pytest.mark.parametrize("stop", [False, True])
async def test_later_invalid_or_skipped_call_cannot_reuse_prior_observation(tmp_path, monkeypatch, stop):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    script_rounds(runtime, monkeypatch, [[("first", "{}"), ("second", "{broken")]])
    called = []
    async with runtime.open() as (app, _pilot):
        async def execute(_name, _args):
            called.append(True)
            app._stop_requested = stop
            return "Host returned normally", True
        monkeypatch.setattr(app, "_execute_tool", execute)
        await run_inline(app)
        events = [row["event"] for row in observations(runtime)]
        assert called == [True]
        assert len(events) == 2
        assert events[0]["provider_call_id"] == "first"
        assert events[1]["observation_id"] == events[0]["observation_id"]
        assert events[1]["result"] == "Host returned normally"
        results = [row for row in app.conversation if row.get("role") == "tool"]
        assert len(results) == 2
        assert ("not executed" if stop else "invalid tool arguments") in results[1]["content"]


@pytest.mark.parametrize("fail_kind", sorted(KINDS))
async def test_observation_emit_failure_propagates_without_retry(tmp_path, monkeypatch, fail_kind):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    script_rounds(runtime, monkeypatch, [[("call", "{}")]])
    called, attempts = [], []
    async with runtime.open() as (app, _pilot):
        original_emit = app._rpc_emit

        def emit(event, **kwargs):
            if event["type"] == fail_kind:
                attempts.append(event)
                raise OSError("fixture observation write failed")
            original_emit(event, **kwargs)

        async def execute(_name, _args):
            called.append(True)
            return "Host result", True

        monkeypatch.setattr(app, "_rpc_emit", emit)
        monkeypatch.setattr(app, "_execute_tool", execute)
        with pytest.raises(OSError, match="observation write failed"):
            await run_inline(app)
        assert len(attempts) == 1
        assert len(called) == (1 if fail_kind == "generic_tool_returned" else 0)
        assert not any(row.get("role") == "tool" for row in app.conversation)
        assert len(observations(runtime)) == (1 if called else 0)
