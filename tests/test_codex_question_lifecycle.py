import asyncio
from threading import Event
from types import SimpleNamespace as NS

import pytest

from litetui import ask_user_question as auq
from litetui.codex_app_server import AppServerTransport
from litetui.codex_question_requests import QuestionRequests
from litetui.question_result import current_lifetime, question_lifetime


def request(ident=1, blocking=True):
    return {
        "id": ident,
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread",
            "turnId": "turn",
            "itemId": "question",
            "isBlocking": blocking,
            "questions": [
                {
                    "id": "q",
                    "header": "Choose",
                    "question": "Which?",
                    "options": [{"label": "One"}, {"label": "Two"}],
                }
            ],
        },
    }


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


def fixture():
    emitted, replies, calls = [], [], []
    app = NS(
        _rpc=True, is_running=True, _stop_requested=False, _rpc_emit=emitted.append
    )

    async def execute(name, args):
        calls.append(name)
        return await asyncio.to_thread(auq.run, args, app), True

    async def send(message):
        replies.append(message)

    async def native_request(method, params):
        calls.append(method)
        return {}

    app._execute_tool = execute
    transport = AppServerTransport(
        NS(send=send, request=native_request, events=asyncio.Queue()), app
    )
    transport.thread_id, transport.turn_id = "thread", "turn"
    return transport, emitted, replies, calls


@pytest.mark.asyncio
async def test_question_does_not_block_dispatch_and_close_releases_real_tool_thread():
    transport, emitted, replies, calls = fixture()
    manager = QuestionRequests(transport)
    assert manager.dispatch(request())
    await eventually(lambda: emitted)
    ident = emitted[0]["id"]
    assert {key: emitted[0][key] for key in (
        "eventVersion", "provider", "threadId", "turnId", "itemId", "delivery"
    )} == {
        "eventVersion": 1, "provider": "codex", "threadId": "thread",
        "turnId": "turn", "itemId": "question", "delivery": "request",
    }
    assert not replies
    assert manager.dispatch(request())  # no duplicate UI or execution
    assert calls == ["ask_user_question"]
    await asyncio.wait_for(manager.close(), 2)
    assert not transport.app._rpc_pending_asks
    assert not replies  # native turn has ended: no stale reply into it
    assert emitted[-1] == {
        "type": "user_input_resolved",
        "id": ident,
        "cancelled": True,
    }
    assert not auq.resolve_over_rpc(transport.app, ident, "submit", [{"selected": [0]}])
    assert not transport.app._stop_requested  # cleanup is not a new user Stop
    assert current_lifetime() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("blocking", [True, False])
async def test_cancel_only_stops_blocking_question_and_reply_is_once(blocking):
    transport, emitted, replies, calls = fixture()
    manager = QuestionRequests(transport)
    manager.dispatch(request(blocking=blocking))
    await eventually(lambda: emitted)
    ident = emitted[0]["id"]
    assert auq.resolve_over_rpc(transport.app, ident, "cancel", [])
    await eventually(lambda: replies)
    assert replies == [{"id": 1, "result": {"answers": {}}}]
    assert transport.app._stop_requested is blocking
    assert "turn/interrupt" not in calls  # the native reader owns interruption
    manager.dispatch(request(blocking=blocking))
    await manager.close()
    assert len(replies) == 1


@pytest.mark.asyncio
async def test_parallel_question_scopes_do_not_cross_answers():
    transport, emitted, replies, _ = fixture()
    manager = QuestionRequests(transport)
    manager.dispatch(request(1, False))
    manager.dispatch(request(2, False))
    await eventually(lambda: len(emitted) == 2)
    for event in reversed(emitted.copy()):
        assert auq.resolve_over_rpc(
            transport.app,
            event["id"],
            "submit",
            [{"selected": [1], "note": event["id"]}],
        )
    await eventually(lambda: len(replies) == 2)
    assert {r["id"] for r in replies} == {1, 2}
    notes = {r["result"]["answers"]["q"]["answers"][1] for r in replies}
    assert notes == {e["id"] for e in emitted}
    await manager.close()


@pytest.mark.asyncio
async def test_stale_request_is_unanswered_without_reopening_previous_turn_ui():
    transport, emitted, replies, calls = fixture()
    manager = QuestionRequests(transport)
    stale = request()
    stale["params"]["turnId"] = "previous-turn"
    manager.dispatch(stale)
    await eventually(lambda: replies)
    assert replies == [{"id": 1, "result": {"answers": {}}}]
    assert not emitted and not calls
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("style", ["modal", "sidebar"])
async def test_native_cancellation_closes_only_its_real_textual_question(style):
    from textual.app import App

    app = App()
    app.settings = NS(dialog_style=style, dialog_side="right")
    cancelled = Event()
    args = {"questions": [{"question": "Choose", "options": [{"title": "One"}]}]}
    async with app.run_test(size=(100, 35)) as pilot:
        with question_lifetime(cancelled):
            task = asyncio.create_task(asyncio.to_thread(auq.run, args, app))
        try:
            await eventually(lambda: bool(app.screen.query(auq.AskUserQuestionBody)))
            cancelled.set()
            result = await asyncio.wait_for(task, 2)
            assert "ABORTED" in result
            await pilot.pause()
            assert not app.screen.query(auq.AskUserQuestionBody)
        finally:
            cancelled.set()
            await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["disconnect", "stop"])
async def test_real_stream_reader_delivers_text_during_question_and_closes(ending):
    from test_codex_app_server import Server

    from litetui.model_transport import ProviderError

    transport, emitted, _, _ = fixture()
    server = Server()
    server.finish = False
    transport.server = server
    app = transport.app
    app.plugins = NS(deferred_specs=list)
    app.backend = NS(models={})
    app.tools_enabled = True
    app.conversation = []
    stream = await transport.create(
        model="gpt-6-astra",
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
    )
    text = []

    async def consume():
        async for chunk in stream:
            if chunk.choices:
                text.append(chunk.choices[0].delta.content or "")

    reader = asyncio.create_task(consume())
    try:
        await eventually(lambda: "OK" in text)
        question = request(blocking=False)
        question["params"].update(threadId="thread-1", turnId="1")
        await server.events.put(question)
        await eventually(
            lambda: any(e["type"] == "user_input_requested" for e in emitted)
        )
        await server.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "1",
                    "delta": "continues while unanswered",
                },
            }
        )
        await eventually(lambda: "continues while unanswered" in text)
        if ending == "disconnect":
            await server.events.put(ProviderError("synthetic disconnect"))
            with pytest.raises(ProviderError, match="synthetic disconnect"):
                await asyncio.wait_for(reader, 2)
        else:
            app._stop_requested = True
            await eventually(
                lambda: any(method == "turn/interrupt" for method, _ in server.requests)
            )
            assert (
                not app._rpc_pending_asks
            )  # released before waiting on native completion
            await server.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "1", "status": "interrupted"},
                    },
                }
            )
            await asyncio.wait_for(reader, 2)
        assert not app._rpc_pending_asks
        assert any(
            e["type"] == "user_input_resolved" and e.get("cancelled") for e in emitted
        )
    finally:
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
