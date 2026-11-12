"""Request-local recovery through the real agent loop; no network or real delays."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
from types import SimpleNamespace as NS

import pytest
from _owned_seat import owned_app
from test_codex_native_tps_wiring import _NativeBackend

from litetui import model_transport as mt


def chunk(text=None, *, tool=False, reasoning=None):
    calls = [NS(index=0, id="effect-1", function=NS(name="write", arguments='{}'))] if tool else []
    return NS(choices=[NS(delta=NS(content=text, reasoning_content=reasoning,
                                  reasoning=None, tool_calls=calls))], usage=None)


def failure(kind="interrupted"):
    if kind == "interrupted":
        return mt.ResponseStream(None, None, "codex", "gpt-6-astra", [])._interrupted()
    return mt._stream_failure("codex", {"type": "error", "code": kind}, ())


class _Emitted(list):
    """RPC events in order; `emit` is bound, so a test can reach the list via `__self__`."""

    def emit(self, row, *, origin=None):  # the host passes each event's origin
        self.append(row)


class Stream:
    def __init__(self, items):
        self.items = items
        self.closed = False

    async def __aiter__(self):
        for item in self.items:
            if callable(item):
                result = item()
                if inspect.isawaitable(result):
                    await result
            elif isinstance(item, Exception):
                raise item
            else:
                yield item

    async def close(self):
        self.closed = True


async def run_turn(monkeypatch, plans, *, source="harness", native=False, transport=None, on_app=None):
    app = owned_app(initial_backend="codex", initial_model="gpt-6-astra", initial_thinking="high")
    app.model_id = "gpt-6-astra"
    app.backend = _NativeBackend()
    # The seat pins "high"; a real Codex model list reports the efforts it supports. An owned
    # invocation level the model does not report is refused before any request is sent.
    app.backend.reasoning_levels = lambda _model: ["high"]
    if not native:
        del app.backend.app_server
        app.backend.shutdown = lambda: None
    app.seat.registered = True
    app._inbox_monitor = lambda: None
    app._connect = lambda: None
    app._apply_cli_args = lambda: None
    app._fetch_ctx_window = lambda: None
    app._hook_source = source
    app.settings.autocompact_enabled = False
    app.settings.tool_iterations = 2
    app.settings.error_message_style = "detail"
    app.tools_enabled = True
    app._kick_card_summary = lambda *a: None
    notices, requests, streams, effects, rpc = [], [], [], [], _Emitted()
    app._report_spawner_error = lambda *args: notices.append(args)
    app._rpc_emit = rpc.emit

    async def ready():
        pass

    async def execute(name, args):
        effects.append(name)
        return "effect completed", True

    async def create(**kwargs):
        requests.append(copy.deepcopy(kwargs["messages"]))
        if transport is not None:
            stream = await transport.create(**kwargs)
            streams.append(stream)
            return stream
        plan = plans[min(len(requests) - 1, len(plans) - 1)]
        if isinstance(plan, Exception):
            raise plan
        stream = Stream(plan)
        streams.append(stream)
        return stream

    app._ensure_chat_ready = ready
    app._execute_tool = execute
    monkeypatch.setattr(mt, "for_app", lambda _: NS(create=create))
    if on_app is not None:
        on_app(app)
    async with app.run_test(size=(120, 38)) as pilot:
        app._materialise_convo()
        app._append({"role": "user", "content": "do the work"})
        worker = app._stream()
        await worker.wait()
        await pilot.pause()
        history = copy.deepcopy(app.conversation)
        transcript = app._agent_session.conversation_directory(app.convo_id) / "convo.jsonl"
        saved = transcript.read_text(encoding="utf-8")
        # Preserve a picture of the actual final pane for the recovery tests.
        pane = app.export_screenshot()
    return NS(history=history, saved=saved, notices=notices, requests=requests,
              streams=streams, effects=effects, rpc=rpc, pane=pane)


@pytest.fixture
def delays(monkeypatch):
    from litetui import app as app_mod
    original = app_mod.asyncio.sleep
    seen = []

    async def sleep(delay, *args, **kwargs):
        if delay in (2.0, 4.0):
            seen.append(delay)
            return
        await original(delay, *args, **kwargs)

    monkeypatch.setattr(app_mod.asyncio, "sleep", sleep)
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["interrupted", "server_error"])
async def test_unattended_recovers_same_request_without_repeating_completed_tool(monkeypatch, delays, kind):
    result = await run_turn(monkeypatch, [
        [chunk(tool=True)],
        [chunk("DISCARDED DRAFT", reasoning="DISCARDED REASONING"), failure(kind)],
        [chunk("finished")],
    ])
    assert len(result.requests) == 3
    assert delays == [2.0]
    assert result.requests[1] == result.requests[2]
    assert result.effects == ["write"]
    assert any(m.get("role") == "tool" and m.get("content") == "effect completed"
               for m in result.requests[2])
    assert result.history[-1]["content"] == "finished"
    assert "DISCARDED" not in json.dumps(result.history)
    assert "DISCARDED" not in result.saved
    assert "DISCARDED" not in json.dumps(result.rpc)
    assert result.notices == []
    assert all(s.closed for s in result.streams)
    assert [e["stopReason"] for e in result.rpc if e["type"] == "turn_end"] == ["stop"]


@pytest.mark.asyncio
async def test_exhaustion_sends_one_notice_and_ends(monkeypatch, delays):
    result = await run_turn(monkeypatch, [[chunk("DISCARDED DRAFT", reasoning="DISCARDED REASONING", tool=True), failure()]])
    assert len(result.requests) == 3
    assert delays == [2.0, 4.0]
    assert len(result.notices) == 1
    assert "interrupted" in result.notices[0][0]
    assert result.notices[0][1] == "model response"
    assert "DISCARDED" not in result.saved
    assert "DISCARDED" not in json.dumps(result.history)
    assert "DISCARDED" not in json.dumps(result.rpc)
    assert result.effects == []
    assert all(s.closed for s in result.streams)
    assert [e["stopReason"] for e in result.rpc if e["type"] == "turn_end"] == ["error"]


@pytest.mark.asyncio
async def test_native_failure_after_executed_tool_is_not_retried(monkeypatch, delays):
    executed = []
    result = await run_turn(monkeypatch, [[lambda: executed.append("effect"), failure()]], native=True)
    assert executed == ["effect"]
    assert len(result.requests) == 1
    assert delays == []
    assert len(result.notices) == 1


@pytest.mark.asyncio
async def test_attended_failure_keeps_existing_behavior(monkeypatch, delays):
    result = await run_turn(monkeypatch, [[chunk("partial"), failure()]], source="typed")
    assert len(result.requests) == 1
    assert delays == []
    assert len(result.notices) == 1
    assert any(e.get("text") == "partial" for e in result.rpc)


@pytest.mark.asyncio
async def test_open_failure_can_recover(monkeypatch, delays):
    result = await run_turn(monkeypatch, [failure("server_error"), [chunk("finished")]])
    assert len(result.requests) == 2
    assert result.history[-1]["content"] == "finished"
    assert delays == [2.0]
    assert result.notices == []


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["context_length_exceeded", "usage_limit_reached", "approval_refused"])
async def test_nontransient_errors_never_retry(monkeypatch, delays, code):
    result = await run_turn(monkeypatch, [[failure(code)]])
    assert len(result.requests) == 1
    assert delays == []
    assert len(result.notices) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["scheduled", "child-result"])
async def test_other_unattended_sources_recover(monkeypatch, delays, source):
    result = await run_turn(monkeypatch, [[failure()], [chunk("finished")]], source=source)
    assert len(result.requests) == 2
    assert result.history[-1]["content"] == "finished"


@pytest.mark.asyncio
async def test_failed_pending_tool_call_is_discarded_not_executed(monkeypatch, delays):
    result = await run_turn(monkeypatch, [[chunk(tool=True), failure()], [chunk("finished")]])
    assert len(result.requests) == 2
    assert result.effects == []
    assert not any(m.get("tool_calls") for m in result.history)
    assert "effect-1" not in result.saved


@pytest.mark.asyncio
async def test_http_and_stream_failures_share_three_attempt_budget(monkeypatch, delays, tmp_path):
    import httpx
    from test_codex_transport_errors import transport, sse, TEXT, DONE
    wire = []

    def handle(request):
        wire.append(json.loads(request.content))
        if len(wire) == 1:
            return httpx.Response(503)
        if len(wire) == 2:
            return httpx.Response(200, content=sse(TEXT, {"type": "error", "code": "server_error"}))
        return httpx.Response(200, content=sse({"type": "response.output_text.delta", "delta": "finished"}, DONE))

    result = await run_turn(monkeypatch, [], transport=transport(tmp_path, handle))
    assert len(wire) == 3 and wire[0] == wire[1] == wire[2]
    assert delays == [2.0, 4.0]
    assert result.history[-1]["content"] == "finished"
    assert result.notices == []
    assert "hello" not in json.dumps(result.rpc)
    assert all(s.client.is_closed for s in result.streams)


@pytest.mark.asyncio
async def test_stop_during_backoff_does_not_restart(monkeypatch):
    original = asyncio.sleep
    app_ref = []

    async def sleep(delay, *args, **kwargs):
        if delay == 2.0:
            app_ref[0]._stop_requested = True
            return
        await original(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    result = await run_turn(monkeypatch, [[failure()]], on_app=app_ref.append)
    assert len(result.requests) == 1
    assert result.notices == []
    assert [e["stopReason"] for e in result.rpc if e["type"] == "turn_end"] == ["cancelled"]


@pytest.mark.asyncio
async def test_pane_streams_live_while_rpc_waits_for_success(monkeypatch, delays, tmp_path):
    app_ref = []

    async def inspect_live_pane():
        await asyncio.sleep(0.1)  # let the real StreamSink paint its next frame
        app = app_ref[0]
        assert "live draft" in str(app._active_turn_widget.body.content)
        assert not any(e.get("text") == "live draft" for e in app._rpc_emit.__self__)
        (tmp_path / "live-pane.svg").write_text(app.export_screenshot(), encoding="utf-8")

    result = await run_turn(monkeypatch, [
        [chunk("live draft"), inspect_live_pane, failure()],
        [chunk("finished")],
    ], on_app=app_ref.append)
    assert "live draft" not in json.dumps(result.rpc)
    assert any(e.get("text") == "finished" for e in result.rpc)
    (tmp_path / "recovered-pane.svg").write_text(result.pane, encoding="utf-8")
