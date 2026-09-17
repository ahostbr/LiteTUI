"""One-line card summary: the side call, the binding, and persistence.

RYAN, 2026-09-16: when a response finishes, ask the SAME model for a one-line
summary with reasoning off, and title the card "<summary> - <model>".

The property this file exists to protect is the BINDING. The call completes
asynchronously, so by the time it returns the user may have switched models or
sent more turns. A summary that lands on "the current card" instead of the card
it was asked about is the defect that would look like the feature working.
"""

import json

import pytest

from litetui import app as app_mod
from litetui.conversation import ConversationRepository
from litetui.widgets import AssistantMessage


def _repo(path):
    """A repository pointed at a scratch file.

    The write guard is `convo_path` plus the loading/pending flags, not a `path`
    attribute - set them the way the app does rather than inventing a double.
    """
    repo = ConversationRepository()
    repo.convo_path = path
    repo.loading = False
    repo.pending = False
    return repo


def _app():
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    return app


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class TestKey:
    def test_key_is_stable_for_the_same_answer(self):
        k = app_mod.LiteTUI._card_summary_key
        assert k("hello world") == k("hello world")

    def test_key_ignores_surrounding_whitespace(self):
        k = app_mod.LiteTUI._card_summary_key
        assert k("  hello  ") == k("hello")

    def test_different_answers_get_different_keys(self):
        k = app_mod.LiteTUI._card_summary_key
        assert k("one") != k("two")


@pytest.mark.asyncio
async def test_summary_titles_the_card_that_asked_for_it(monkeypatch):
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        card = AssistantMessage()
        card.set_model_name("qwen3-30b")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()

        async def fake_create(**kw):
            return _Resp("Explained the parkour component")
        monkeypatch.setattr(app_mod.model_transport, "for_app",
                            lambda _a: type("T", (), {"create": staticmethod(fake_create)})())

        await app._summarise_card(card, "qwen3-30b", "some long answer")
        assert card.border_title.endswith("Explained the parkour component - qwen3-30b")


@pytest.mark.asyncio
async def test_a_late_summary_cannot_stamp_a_newer_card(monkeypatch):
    """THE BINDING. Two cards, two models; the FIRST card's summary lands last.

    It must title card one with model one, leaving card two untouched - even
    though the app's own `model_id` has moved on.
    """
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        first = AssistantMessage(); first.set_model_name("model-A")
        second = AssistantMessage(); second.set_model_name("model-B")
        log = app.query_one("#chat-log")
        log.mount(first); log.mount(second)
        await pilot.pause()

        app.model_id = "model-B"          # the user switched models meanwhile
        seen = {}

        async def fake_create(**kw):
            seen["model"] = kw.get("model")
            return _Resp("First card answer")
        monkeypatch.setattr(app_mod.model_transport, "for_app",
                            lambda _a: type("T", (), {"create": staticmethod(fake_create)})())

        await app._summarise_card(first, "model-A", "answer one")

        assert seen["model"] == "model-A", "must ask the model that answered, not the current one"
        assert "First card answer" in first.border_title
        assert "model-A" in first.border_title
        assert second.summary is None, "a late summary must not touch another card"


@pytest.mark.asyncio
async def test_failure_leaves_the_model_name_standing(monkeypatch):
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        card = AssistantMessage(); card.set_model_name("qwen3-30b")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()

        async def boom(**kw):
            raise RuntimeError("backend down")
        monkeypatch.setattr(app_mod.model_transport, "for_app",
                            lambda _a: type("T", (), {"create": staticmethod(boom)})())

        await app._summarise_card(card, "qwen3-30b", "answer")
        assert card.summary is None
        assert card.border_title.endswith("qwen3-30b")


@pytest.mark.asyncio
async def test_empty_reply_leaves_the_model_name_standing(monkeypatch):
    app = _app()
    async with app.run_test(size=(100, 20)) as pilot:
        card = AssistantMessage(); card.set_model_name("qwen3-30b")
        app.query_one("#chat-log").mount(card)
        await pilot.pause()

        async def empty(**kw):
            return _Resp("   ")
        monkeypatch.setattr(app_mod.model_transport, "for_app",
                            lambda _a: type("T", (), {"create": staticmethod(empty)})())

        await app._summarise_card(card, "qwen3-30b", "answer")
        assert card.summary is None
        assert card.border_title.endswith("qwen3-30b")


class TestKick:
    def test_no_answer_text_never_asks(self):
        app = _app()
        card = AssistantMessage()
        card.answer_text = ""
        app._kick_card_summary(card)          # must not raise, must not schedule
        assert card.summary_done is False

    def test_an_already_summarised_card_is_not_reasked(self):
        app = _app()
        card = AssistantMessage()
        card.answer_text = "something"
        card.set_summary("restored from disk")
        app._kick_card_summary(card)
        assert card.summary_done is True       # marked, not re-requested


class TestPersistence:
    def test_record_and_read_back(self, tmp_path):
        path = tmp_path / "convo.jsonl"
        path.write_text(json.dumps({"type": "meta", "name": "t"}) + "\n", encoding="utf-8")
        repo = _repo(path)
        repo.record_card_summary("abc123", "Did the thing", "model-A")
        assert ConversationRepository.card_summaries(path) == {"abc123": "Did the thing"}

    def test_last_record_wins(self, tmp_path):
        path = tmp_path / "convo.jsonl"
        path.write_text("", encoding="utf-8")
        repo = _repo(path)
        repo.record_card_summary("k", "first", "m")
        repo.record_card_summary("k", "second", "m")
        assert ConversationRepository.card_summaries(path)["k"] == "second"

    def test_summary_records_are_invisible_to_the_message_reader(self, tmp_path):
        """The record must never reach the model's context."""
        path = tmp_path / "convo.jsonl"
        rows = [
            {"type": "meta", "name": "t"},
            {"type": "msg", "ts": 1, "message": {"role": "user", "content": "hi"}},
            {"type": "card_summary", "ts": 2, "key": "k", "summary": "s", "model": "m"},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        _meta, msgs = ConversationRepository.read(path)
        assert [m["role"] for m in msgs] == ["user"]
        assert all("summary" not in m for m in msgs)

    def test_missing_file_is_empty_not_an_error(self, tmp_path):
        assert ConversationRepository.card_summaries(tmp_path / "nope.jsonl") == {}


class TestCardinality:
    """Exactly one summary request per settled card, and never a second.

    `all(m == sentinel for m in sent)` in test_chat_ready_call_sites cannot
    express this - it is a membership check and passes on an empty list. The
    count belongs here, where the worker can be awaited deterministically.

    The scheduling is intercepted rather than waited on: `workers.wait_for_
    complete()` waits for EVERY worker the app owns, several of which are
    long-lived, so it hangs instead of settling. Capturing the coroutine keeps
    `_kick_card_summary`'s guards under test while making completion exact.
    """

    @pytest.mark.asyncio
    async def test_one_request_per_card_and_no_repeat(self, monkeypatch):
        app = _app()
        async with app.run_test(size=(100, 20)) as pilot:
            card = AssistantMessage()
            card.set_model_name("model-A")
            app.query_one("#chat-log").mount(card)
            await pilot.pause()
            card.set_answer("an answer worth one line")
            card.settled = True

            calls: list = []

            async def fake_create(**kw):
                calls.append(kw.get("model"))
                return _Resp("One line")
            monkeypatch.setattr(app_mod.model_transport, "for_app",
                                lambda _a: type("T", (), {"create": staticmethod(fake_create)})())

            scheduled: list = []
            monkeypatch.setattr(app, "run_worker",
                                lambda coro, **kw: scheduled.append(coro))

            app._kick_card_summary(card)
            for coro in scheduled:
                await coro
            assert calls == ["model-A"], f"expected exactly one summary call: {calls!r}"

            # A second settle (or a redraw) must not pay for it again.
            scheduled.clear()
            app._kick_card_summary(card)
            assert scheduled == [], "a second kick scheduled another request"
            assert calls == ["model-A"], f"summary was requested twice: {calls!r}"

    @pytest.mark.asyncio
    async def test_restored_card_makes_no_request(self, monkeypatch):
        """Reopen persistence: a card that already carries a summary is silent."""
        app = _app()
        async with app.run_test(size=(100, 20)) as pilot:
            card = AssistantMessage()
            card.set_model_name("model-A")
            app.query_one("#chat-log").mount(card)
            await pilot.pause()
            card.set_answer("restored answer")
            card.settled = True
            card.set_summary("already known")     # as _resume would set it

            calls: list = []

            async def fake_create(**kw):
                calls.append(kw.get("model"))
                return _Resp("should never happen")
            monkeypatch.setattr(app_mod.model_transport, "for_app",
                                lambda _a: type("T", (), {"create": staticmethod(fake_create)})())

            scheduled: list = []
            monkeypatch.setattr(app, "run_worker",
                                lambda coro, **kw: scheduled.append(coro))
            app._kick_card_summary(card)
            for coro in scheduled:
                await coro
            assert calls == [], f"a restored summary must not be regenerated: {calls!r}"
            assert card.summary == "already known"
