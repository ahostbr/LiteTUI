"""One-line card title: from the model's own <recap>, else the card's own text. NEVER a request.

T1433. Ryan 2026-11-26 (via Sentinel b5049a76): "ya so that summary thing was only ever really
meant to be in the system prompt to depend on the model to output the tags ... i deft never wanted
a extra call for it ... and espically one that clears the cache". The side call this file used to
protect (purpose "card-summary", 2026-09-16) is gone: on a one-slot engine it evicted the whole
cached conversation. What stays: the title on every finished card, and stored summaries on reopen.
"""

import json

import pytest

from litetui import app as app_mod
from litetui.conversation import ConversationRepository
from litetui.widgets import AssistantMessage


def _app():
    from test_compaction_ui import _app as owned_app

    app = owned_app()
    app._inbox_monitor = lambda: None  # fixture registration has no live inbox
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



class TestKick:
    def test_a_card_without_a_recap_is_titled_from_its_own_first_line(self, monkeypatch):
        app = _app()
        monkeypatch.setattr(app_mod.model_transport, "for_app",
                            lambda _a: pytest.fail("a card title must never send a request"))
        scheduled = []
        monkeypatch.setattr(app, "run_worker", lambda *a, **k: scheduled.append(a))
        card = AssistantMessage()
        card.set_model_name("qwen3-30b")
        card.answer_text = "## Fixed the parser\n\nIt reads each line once now."
        app._kick_card_summary(card)
        assert scheduled == []
        assert card.summary == "Fixed the parser" and card.summary_done is True
        assert card.border_title.endswith("Fixed the parser - qwen3-30b")

    def test_no_answer_and_no_trace_keeps_the_model_name(self):
        app = _app()
        card = AssistantMessage()
        card.set_model_name("qwen3-30b")
        card.answer_text = ""
        app._kick_card_summary(card)          # must not raise, must not schedule
        assert card.summary is None
        assert card.border_title.endswith("qwen3-30b")

    def test_an_already_summarised_card_is_not_reasked(self):
        app = _app()
        card = AssistantMessage()
        card.answer_text = "something"
        card.set_summary("restored from disk")
        app._kick_card_summary(card)
        assert card.summary_done is True       # marked, not re-requested


def _old_summary_rows(path, *rows):
    """card_summary records as builds before T1433 wrote them (nothing writes them now)."""
    path.write_text("\n".join(json.dumps(r) for r in (
        {"type": "meta", "name": "t"},
        *({"type": "card_summary", "ts": 1, "key": k, "summary": s, "model": "m"} for k, s in rows))) + "\n",
        encoding="utf-8")


class TestPersistence:
    def test_an_old_record_is_read_back(self, tmp_path):
        path = tmp_path / "convo.jsonl"
        _old_summary_rows(path, ("abc123", "Did the thing"))
        assert ConversationRepository.card_summaries(path) == {"abc123": "Did the thing"}

    def test_last_record_wins(self, tmp_path):
        path = tmp_path / "convo.jsonl"
        _old_summary_rows(path, ("k", "first"), ("k", "second"))
        assert ConversationRepository.card_summaries(path)["k"] == "second"

    def test_nothing_writes_a_summary_record_any_more(self):
        assert not hasattr(ConversationRepository, "record_card_summary")

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
    """No request for a settled card, ever: not the first settle, not a second (T1433).

    The scheduling is intercepted rather than waited on: `workers.wait_for_
    complete()` waits for EVERY worker the app owns, several of which are
    long-lived, so it hangs instead of settling.
    """

    @pytest.mark.asyncio
    async def test_no_request_per_card_and_no_repeat(self, monkeypatch):
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
            assert calls == [] and scheduled == [], f"a settled card sent a request: {calls!r}"
            assert card.summary == "an answer worth one line"

            # A second settle (or a redraw) changes nothing either.
            app._kick_card_summary(card)
            assert scheduled == [] and calls == []

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


# ── every round, not only the last (the user, 2026-09-18) ──────────────────────
#
# "not all responses are getting a summary ... even ones that produce both
# thinking and a response ... also we need to figure out the just thinking
# ones how to display something for those also" — and, watching it: "it seems
# like only messages he does a full stop on get the summary".
#
# The settle path ran once per TURN, so in an agentic turn every card that
# ended in a tool call kept its model-name header for good, and a card that
# held only a thinking block had nothing to title it at all.

import asyncio
from types import SimpleNamespace

from litetui import paths
from litetui.settings import Settings
from litetui.tool_policy import SHELL_POLICY


class TestThinkingOnly:
    def test_a_thinking_only_card_titles_itself_from_its_trace(self, monkeypatch):
        app = _app()
        scheduled = []
        monkeypatch.setattr(app, "run_worker", lambda *a, **k: scheduled.append(a))
        card = AssistantMessage()
        card.set_model_name("qwen3-30b")
        card.thinking = SimpleNamespace(
            source="I need to check the structure of a single conversation. Then size it.")
        app._kick_card_summary(card)
        assert scheduled == [], "a trace is never sent out for a side call"
        assert card.summary == "thinking: I need to check the structure of a single conversation."
        assert card.summary_done is True
        assert card.border_title.endswith("- qwen3-30b")

    def test_a_trace_without_punctuation_is_still_a_header(self):
        assert app_mod._first_sentence("  just   thinking\n out loud ") == "just thinking out loud"
        assert app_mod._first_sentence("") == ""


# -- a three-round agentic turn, driven end to end -------------------------

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, index=0, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.reasoning = None
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, **kw):
        self.choices = [type("C", (), {"delta": _Delta(**kw), "finish_reason": None})()]
        self.usage = None


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        # A real first token is a network round-trip away, and the new bubble
        # has mounted by then. A fake that yields on the same tick reaches the
        # thinking mount before the card is attached (MountError).
        await asyncio.sleep(0.01)
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        pass


def _probe(n):
    return [_Chunk(tool_calls=[_TC(0, id=f"c{n}", name="probe")]),
            _Chunk(tool_calls=[_TC(0, arguments="{}")])]


@pytest.mark.asyncio
async def test_every_finished_card_in_a_turn_gets_its_line(monkeypatch, tmp_path):
    """Round 1: answer + tool call. Round 2: thinking only + tool call.
    Round 3: the final answer. Every card gets its line, and no side call is made (T1433)."""
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path)
    app = _app()
    app.settings = Settings(tools_enabled=True, tool_iterations=5,
                            autocompact_enabled=False, wake_after_compact=False,
                            clear_screen_after_compact=False)
    app.tools_enabled = True
    app.model_id = "fixture"
    app.available_models = ["fixture"]
    said = []
    app._system = lambda msg, *a, **k: said.append(str(msg))

    async def ready():
        pass
    app._ensure_chat_ready = ready
    app.plugins.add_tool(
        "test",
        {"type": "function", "function": {"name": "probe", "description": "t",
                                          "parameters": {"type": "object", "properties": {}}}},
        lambda args: "ran", policy=SHELL_POLICY)

    rounds = iter([
        _Stream([_Chunk(content="Looking first.")] + _probe(1)),
        _Stream([_Chunk(reasoning_content="Now the second probe. More follows.")] + _probe(2)),
        _Stream([_Chunk(content="Done.")]),
    ])
    side_calls = []

    async def create(**kw):
        purpose = kw.get("purpose", "turn")
        if purpose != "turn":                 # any side call at all: recorded, one canned line
            side_calls.append(purpose)
            return _Resp("one line")
        return next(rounds)
    monkeypatch.setattr(app_mod.model_transport, "for_app",
                        lambda _a: SimpleNamespace(create=create))

    async with app.run_test(size=(100, 40)) as pilot:
        app._append({"role": "user", "content": "go"})
        app._stream()
        for _ in range(200):
            await pilot.pause()
            if not app._chat_running():
                break
        for _ in range(10):
            await pilot.pause()
        cards = list(app.query(AssistantMessage))

    assert len(cards) == 3, ([c.answer_text for c in cards], said)
    assert all(c.settled for c in cards)
    assert side_calls == [], side_calls            # T1433: no side request, any round
    assert cards[0].summary == "Looking first."
    assert cards[1].summary == "thinking: Now the second probe."
    assert cards[2].summary == "Done."
