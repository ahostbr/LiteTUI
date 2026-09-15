import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

from litetui import ask_user_question as auq
from litetui.codex_async_questions import AsyncQuestions, answer_text
from litetui.model_transport import ProviderError


@pytest.fixture(autouse=True)
def no_real_app_server(monkeypatch):
    from litetui.codex_app_server import AppServer

    async def refuse(_):
        pytest.fail(
            "Async-question tests must use the synthetic server, never a real process"
        )

    monkeypatch.setattr(AppServer, "start", refuse)


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def fixture():
    metadata = {"provider": "codex", "app_server_thread_id": "native"}
    emitted, saved = [], []
    app = NS(
        _rpc=True,
        is_running=True,
        _stop_requested=False,
        convo_id="conversation",
        conversation=[
            {"role": "user", "content": "hello", "provider_metadata": metadata}
        ],
        _pending_input=[],
        chosen_tool_profile="interactive",
        _rpc_emit=emitted.append,
        _edit=lambda *_: saved.append(copy.deepcopy(metadata)),
    )

    async def execute(name, args):
        assert name == "ask_user_question"
        return await asyncio.to_thread(auq.run, args, app), True

    app._execute_tool = execute
    transport = NS(app=app, server=NS(), thread_id="native", turn_id="turn")
    return AsyncQuestions(transport), metadata, emitted, saved


ITEM = {
    "id": "ask-item",
    "type": "agentMessage",
    "delivery": "async",
    "text": "Which option?",
    "questions": [{"title": "Which option?", "options": ["One", "Two"]}],
}


@pytest.mark.asyncio
async def test_saved_question_card_tracks_delivery_outcome():
    from textual.app import App

    from litetui.codex_question_card import SavedQuestionCard

    entry = {"id": "question", "threadId": "native", "state": "answered",
             "deliveryId": "delivery", "questions": ITEM["questions"]}
    delivery = {"id": "delivery", "threadId": "native", "state": "queued"}
    metadata = {"async_questions": [entry], "steering": [delivery]}
    app = App()
    app.conversation = [{"provider_metadata": metadata}]
    app.store = NS(persist_error=None)
    async with app.run_test() as pilot:
        card = SavedQuestionCard(metadata, entry)
        await app.mount(card)
        await pilot.pause()
        for state, label in (("queued", "Answer queued"), ("accepted", "Answer accepted by Codex"),
                             ("denied", "Answer not sent"), ("uncertain", "Answer delivery unconfirmed")):
            delivery["state"] = state
            card.refresh_state()
            assert str(card.answer.label) == label
            assert card.answer.disabled
        app.store.persist_error = "synthetic disk failure"
        card.refresh_state()
        assert str(card.answer.label) == "Answer state not saved"
        app.store.persist_error = None
        delivery.update(state="accepted", threadId="other")
        card.refresh_state()
        assert str(card.answer.label) == "Answer delivery unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_first", [False, True])
async def test_replay_ignores_pending_snapshot_when_an_answered_copy_exists(terminal_first):
    from textual.app import App
    from textual.containers import VerticalScroll

    from litetui.codex_question_card import SavedQuestionCard
    from litetui.codex_trace import replay

    class ReviewApp(App):
        def compose(self):
            yield VerticalScroll(id="chat-log")

    pending = {
        "app_server_thread_id": "native",
        "async_questions": [{
            "version": 1, "id": "same", "threadId": "native",
            "state": "pending", "questions": ITEM["questions"],
        }],
    }
    answered = copy.deepcopy(pending)
    answered["async_questions"][0]["state"] = "answered"
    snapshots = [answered, pending] if terminal_first else [pending, answered]
    app = ReviewApp()
    app.conversation = [{"provider_metadata": metadata} for metadata in snapshots]
    async with app.run_test() as pilot:
        seen = set()
        for metadata in snapshots:
            replay(app, metadata, seen)
        await pilot.pause()
        assert not app.query(SavedQuestionCard)


@pytest.mark.asyncio
async def test_answer_after_native_turn_ends_is_persisted_before_queue_and_uses_admission():
    manager, metadata, emitted, saved = fixture()
    app = manager.transport.app
    manager.open(ITEM, metadata, 0)
    try:
        await eventually(lambda: emitted)
        assert saved[0]["async_questions"][0]["state"] == "pending"
        manager.transport.turn_id = None  # async questions survive a natural turn end
        assert auq.resolve_over_rpc(
            app, emitted[0]["id"], "submit", [{"selected": [1], "note": "my note"}]
        )
        await eventually(lambda: app._pending_input)
        assert len(app._pending_input) == 1
        queued = app._pending_input[0]
        assert queued["source"] == "codex-question"
        assert "Answer: Two\nmy note" in queued["content"]
        assert (
            saved[-1]["async_questions"][0]["deliveryId"]
            == queued["_codex_entry"]["id"]
        )
        assert saved[-1]["steering"][0]["state"] == "queued"
        calls = []

        async def admit(item):
            calls.append("admission")
            return False, {"reason": "test denial"}

        async def send(*_):
            pytest.fail("a denied async answer must not reach native inference")

        outcome = await queued["_codex_ledger"].deliver(
            queued["_codex_entry"], admit=admit, request=send
        )
        assert outcome == "denied" and calls == ["admission"]
    finally:
        manager.cancel()
        await eventually(lambda: not manager.active)


@pytest.mark.asyncio
async def test_replayed_notification_and_late_answer_cannot_duplicate_question_or_delivery():
    manager, metadata, emitted, _ = fixture()
    app = manager.transport.app
    manager.open(ITEM, metadata, 0)
    manager.open(ITEM, metadata, 0)
    try:
        await eventually(lambda: emitted)
        assert len(emitted) == 1
        ident = emitted[0]["id"]
        assert auq.resolve_over_rpc(app, ident, "submit", [{"selected": [0]}])
        await eventually(lambda: not manager.active)
        manager.open(ITEM, metadata, 0)
        assert len(emitted) == 1 and len(app._pending_input) == 1
        assert not auq.resolve_over_rpc(app, ident, "submit", [{"selected": [1]}])
        entry = metadata["async_questions"][0]
        delivery = app._pending_input.pop()["_codex_entry"]
        delivery["state"] = "accepted"
        manager.queue_answer(entry, metadata, 0, "different late answer")
        assert not app._pending_input
        assert len(metadata["steering"]) == 1
    finally:
        manager.cancel()
        await eventually(lambda: not manager.active)


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["stop", "switch", "disconnect"])
async def test_scope_end_closes_question_but_never_queues_an_inferred_answer(ending):
    manager, metadata, emitted, _ = fixture()
    app = manager.transport.app
    manager.open(ITEM, metadata, 0)
    await eventually(lambda: emitted)
    if ending == "stop":
        app._stop_requested = True
    elif ending == "switch":
        app.convo_id = "another"
    else:
        manager.cancel()
    await eventually(lambda: not manager.active)
    assert not app._rpc_pending_asks and not app._pending_input
    assert metadata["async_questions"][0]["state"] == "pending"
    assert emitted[-1]["type"] == "user_input_resolved"


def test_chat_cancel_and_unanswered_are_not_synthetic_user_messages():
    for action in ("chat", "cancelled", "aborted", "submit"):
        assert (
            answer_text(ITEM["questions"], {"action": action, "questions": []}) is None
        )


def test_save_failure_prevents_queue_visibility():
    manager, metadata, _, _ = fixture()
    app = manager.transport.app
    app._edit = lambda *_: None
    app.store = NS(persist_error="disk unavailable", convo_path="x", loading=False)
    entry = {
        "id": "q",
        "threadId": "native",
        "turnId": "turn",
        "conversationId": "conversation",
    }
    with pytest.raises(ProviderError, match="could not be saved"):
        manager.queue_answer(entry, metadata, 0, "answer")
    assert not app._pending_input


def test_answer_and_delivery_survive_repository_reload_without_duplicate_queue(tmp_path):
    from litetui.codex_steering import restore_queue
    from litetui.conversation import ConversationRepository

    manager, metadata, _, _ = fixture()
    app = manager.transport.app
    store = ConversationRepository()
    store.convo_dir = tmp_path
    store.convo_path = tmp_path / "questions.jsonl"
    app.store = store
    app._edit = lambda index, _: store.record_edit(index, app.conversation[index])
    entry = {
        "version": 1,
        "id": "persisted-question",
        "threadId": "native",
        "turnId": "turn",
        "conversationId": app.convo_id,
        "state": "pending",
        "questions": ITEM["questions"],
    }
    metadata["async_questions"] = [entry]
    try:
        store.record_msg(app.conversation[0])
        manager.queue_answer(entry, metadata, 0, "explicit answer")
        _, app.conversation = ConversationRepository.read(store.convo_path)
        app._pending_input = []
        restore_queue(app)
        restore_queue(app)
        restored = app.conversation[0]["provider_metadata"]
        assert restored["async_questions"][0]["state"] == "answered"
        assert len(app._pending_input) == 1
        queued = app._pending_input[0]
        assert queued["content"] == "explicit answer"
        assert queued["_codex_entry"]["state"] == "queued"
        assert queued["_codex_entry"]["id"] == restored["async_questions"][0]["deliveryId"]
    finally:
        store.release()


@pytest.mark.asyncio
async def test_resume_is_read_only_until_saved_question_button_is_pressed():
    from textual.app import App
    from textual.containers import VerticalScroll

    from litetui.codex_question_card import SavedQuestionCard
    from litetui.codex_trace import replay

    class ReviewApp(App):
        def compose(self):
            yield VerticalScroll(id="chat-log")

    manager, metadata, emitted, _ = fixture()
    app = ReviewApp()
    app._rpc = True
    app.convo_id = "conversation"
    app._stop_requested = (
        True  # a prior stopped turn must not cancel a new explicit answer
    )
    app._pending_input = []
    app._rpc_emit = emitted.append
    app._edit = lambda *_: None
    app.conversation = [{"role": "user", "provider_metadata": metadata}]
    app.backend = NS(
        name="codex", app_server=manager.transport.server, _transport=manager.transport
    )
    calls = []

    async def execute(name, args):
        calls.append(name)
        return await asyncio.to_thread(auq.run, args, app), True

    app._execute_tool = execute
    manager.transport.app = app
    metadata["async_questions"] = [
        {
            "version": 1,
            "id": "saved-question",
            "threadId": "native",
            "turnId": "turn",
            "conversationId": app.convo_id,
            "state": "pending",
            "questions": ITEM["questions"],
        }
    ]
    async with app.run_test(size=(100, 35)) as pilot:
        seen = set()
        replay(app, metadata, seen)
        replay(app, metadata, seen)
        await pilot.pause()
        assert len(app.query(SavedQuestionCard)) == 1
        assert not calls and not emitted and not app._pending_input
        app.query_one(SavedQuestionCard).answer.press()
        try:
            await eventually(lambda: emitted)
            assert calls == ["ask_user_question"]
            assert not app._pending_input
        finally:
            manager.cancel()
            await eventually(lambda: not manager.active)
        card = app.query_one(SavedQuestionCard)
        card.refresh_state()
        assert not card.answer.disabled
        assert str(card.answer.label) == "Answer question"
        card.answer.press()
        try:
            await eventually(lambda: len(calls) == 2)
        finally:
            manager.cancel()
            await eventually(lambda: not manager.active)
        # A card already on screen must not answer an obsolete pending snapshot.
        latest = copy.deepcopy(metadata)
        latest["async_questions"][0].update(state="answered", revision=1)
        app.conversation.append({"role": "assistant", "provider_metadata": latest})
        card.refresh_state()
        card.answer.press()
        await pilot.pause()
        assert calls == ["ask_user_question", "ask_user_question"]
        assert card.answer.disabled
        with pytest.raises(ProviderError, match="superseded"):
            manager.queue_answer(metadata["async_questions"][0], metadata, 0, "late")
        assert not app._pending_input


@pytest.mark.asyncio
async def test_omitted_stream_sidecall_is_blocked_before_real_process_start():
    from litetui.codex_app_server import AppServerTransport

    transport = AppServerTransport(NS(), NS())
    with pytest.raises(pytest.fail.Exception, match="never a real process"):
        await transport.create(
            model="gpt-6-astra", messages=[{"role": "user", "content": "hello"}]
        )


@pytest.mark.asyncio
async def test_cancelled_waiting_dialog_exits_without_releasing_active_dialog_slot():
    from threading import Event

    from litetui.question_result import question_lifetime, question_slot

    app = NS(_rpc=False)
    cancelled = Event()

    async def waiting():
        with question_lifetime(cancelled):
            async with question_slot(app):
                pytest.fail("cancelled queued question must not open UI")

    async with question_slot(app):
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0.02)
        cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert app._codex_question_ui_lock.locked()
    assert not app._codex_question_ui_lock.locked()


@pytest.mark.asyncio
async def test_native_notification_reaches_shared_ui_and_reply_queue_after_turn_completion():
    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServerTransport

    class AsyncServer(Server):
        async def request(self, method, params):
            self.finish = False
            result = await super().request(method, params)
            if method == "turn/start":
                payload = {"threadId": "thread-1", "turnId": "1", "item": ITEM}
                await self.events.put({"method": "item/started", "params": payload})
                await self.events.put({"method": "item/completed", "params": payload})
                await self.events.put({"method": "item/completed", "params": payload})
                await self.events.put(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "1", "status": "completed"},
                        },
                    }
                )
            return result

    _, _, emitted, _ = fixture()
    app = NS(
        _rpc=True,
        is_running=True,
        _stop_requested=False,
        convo_id="conversation",
        conversation=[{"role": "user", "content": "hello"}],
        _pending_input=[],
        chosen_tool_profile="interactive",
        _rpc_emit=emitted.append,
        _edit=lambda *_: None,
        plugins=NS(deferred_specs=list),
        backend=NS(models={}),
        tools_enabled=True,
    )

    async def execute(_, args):
        return await asyncio.to_thread(auq.run, args, app), True

    app._execute_tool = execute
    server = AsyncServer()
    transport = AppServerTransport(server, app)
    from litetui.model_transport import collect

    response = await collect(
        await transport.create(
            model="gpt-6-astra", messages=app.conversation, stream=True
        )
    )
    manager = server.async_questions
    try:
        assert response.choices[0].message.content.count("Which option?") == 1
        assert transport.turn_id is None
        await eventually(
            lambda: any(event["type"] == "user_input_requested" for event in emitted)
        )
        question = next(
            event for event in emitted if event["type"] == "user_input_requested"
        )
        start = next(event for event in emitted if event["type"] == "native_turn_started")
        assert start["threadId"] == question["threadId"]
        assert start["turnId"] == question["turnId"]
        assert emitted.index(start) < emitted.index(question)
        assert {
            key: question[key]
            for key in ("eventVersion", "provider", "threadId", "turnId", "itemId", "delivery")
        } == {
            "eventVersion": 1, "provider": "codex", "threadId": "thread-1",
            "turnId": "1", "itemId": "ask-item", "delivery": "async",
        }
        assert auq.resolve_over_rpc(app, question["id"], "submit", [{"selected": [0]}])
        await eventually(lambda: app._pending_input)
        assert len(app._pending_input) == 1
        assert app._pending_input[0]["_codex_entry"]["turnId"] == "1"
        assert not server.replies  # this was a notification, never a server request
    finally:
        manager.cancel()
        await eventually(lambda: not manager.active)
