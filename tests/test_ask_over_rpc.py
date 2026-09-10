"""T558-A — ask_user_question answers over the wire instead of deadlocking.

The defect is reproduced in `test_ask_over_rpc_hangs.py`, which was RED-capable
before this existed: headless, the widget path pushes a screen nobody can see and
the tool call waits out the session. These are the arms for the fix.

🔴 THE SHAPE THAT MATTERS IS "UNANSWERED IS AN ANSWER". Three of the arms below
are about what comes back when nobody answered, answered late, or answered
badly — because the caller is a model that will otherwise PROCEED. An empty or
partial answer set that reads like a real one is worse than a hang: the hang is
at least visible.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import ask_user_question as auq  # noqa: E402

TWO_QUESTIONS = {
    "questions": [
        {
            "label": "Approach",
            "question": "Which approach?",
            "options": [
                {"title": "A", "description": "the first"},
                {"title": "B", "description": "the second"},
            ],
        },
        {
            "label": "Timing",
            "question": "When?",
            "options": [{"title": "now"}, {"title": "later"}],
        },
    ]
}


class FakeRpcApp:
    """The smallest thing `_run_over_rpc` needs: a wire and a liveness flag.

    Deliberately NOT a real LiteTUI: the widget path is what needs an app, and
    routing around it is the entire change. A real app here would re-introduce
    the very screen this path exists to avoid.
    """

    def __init__(self) -> None:
        self._rpc = True
        self.is_running = True
        self.emitted: list[dict] = []

    def _rpc_emit(self, data: dict) -> None:
        self.emitted.append(data)


def _ask_in_thread(app, args=TWO_QUESTIONS):
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(auq.run(args, app)), daemon=True)
    t.start()
    return t, out


def _wait_for_ask(app, timeout=3.0) -> dict:
    """The emitted question, once the worker has put it on the wire."""
    deadline = threading.Event()
    for _ in range(int(timeout * 100)):
        if app.emitted:
            return app.emitted[0]
        deadline.wait(0.01)
    raise AssertionError("no user_input_requested was emitted")


def test_the_question_goes_out_on_the_wire_with_an_id():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    try:
        event = _wait_for_ask(app)
        assert event["type"] == "user_input_requested"
        assert event["id"], "no id — the host has nothing to answer"
        # The full question, so the host can render it without a second call.
        assert [q["question"] for q in event["questions"]] == ["Which approach?", "When?"]
        assert event["questions"][0]["options"][0]["title"] == "A"
    finally:
        app.is_running = False
        t.join(timeout=8)


def test_an_answer_releases_the_tool_call_with_the_selections():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    event = _wait_for_ask(app)

    assert auq.resolve_over_rpc(app, event["id"], "submit", [
        {"selected": [1], "note": "because B"},
        {"selected": [0]},
    ])
    t.join(timeout=8)
    assert not t.is_alive(), "the tool call did not return after being answered"

    text = out[0]
    assert "SUBMITTED" in text
    assert "2 of 2 answered" in text
    assert "[x] B" in text
    assert "because B" in text


def test_an_unknown_id_is_refused_rather_than_silently_dropped():
    app = FakeRpcApp()
    # Nothing is pending at all.
    assert auq.resolve_over_rpc(app, "ask-nonexistent", "submit", []) is False


def test_a_second_answer_is_refused():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    event = _wait_for_ask(app)

    assert auq.resolve_over_rpc(app, event["id"], "submit", [{"selected": [0]}])
    t.join(timeout=8)
    # The call has moved on. A late answer must not report success to a host
    # that would then believe the model got it.
    assert auq.resolve_over_rpc(app, event["id"], "submit", [{"selected": [1]}]) is False


def test_the_registry_does_not_leak_the_ask():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    event = _wait_for_ask(app)
    auq.resolve_over_rpc(app, event["id"], "submit", [{"selected": [0]}])
    t.join(timeout=8)
    assert auq._ask_registry(app) == {}, "an answered ask stayed pending"


def test_exiting_without_an_answer_says_UNANSWERED_and_asserts_nothing():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    _wait_for_ask(app)

    app.is_running = False  # the host went away
    t.join(timeout=8)
    assert not t.is_alive(), "the worker was not released when the app stopped"

    text = out[0]
    assert "UNANSWERED" in text
    # The model must not be able to read a selection out of this.
    assert "[x]" not in text
    assert "SUBMITTED" not in text


def test_a_cancel_is_distinct_from_an_answer():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    event = _wait_for_ask(app)

    auq.resolve_over_rpc(app, event["id"], "cancel", [])
    t.join(timeout=8)
    assert "CANCELLED" in out[0]
    assert "SUBMITTED" not in out[0]


def test_a_malformed_answer_reads_as_unanswered_not_as_a_pick():
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    event = _wait_for_ask(app)

    # Out-of-range indices, wrong types, a short list: none of it may invent a
    # selection, and none of it may strand the tool call.
    auq.resolve_over_rpc(app, event["id"], "submit", [
        {"selected": [99, "two", None]},
    ])
    t.join(timeout=8)
    text = out[0]
    assert "[x]" not in text, "an out-of-range index became a selection"
    assert "0 of 2 answered" in text


@pytest.mark.asyncio
async def test_a_real_headless_app_takes_the_wire_path_not_the_widget():
    """The integration end: a REAL LiteTUI in the state --rpc puts it in.

    The FakeRpcApp arms above prove the wire logic. This one proves the ROUTING —
    that `run` chooses the wire when `_rpc` is set, on the real object, which is
    the thing the reproduction showed going the other way.
    """
    a = m.LiteTUI()
    a._rpc = True
    emitted: list[dict] = []
    a._rpc_emit = emitted.append  # type: ignore[method-assign]

    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        t, out = _ask_in_thread(a)
        for _ in range(300):
            if emitted:
                break
            await pilot.pause(0.01)
        assert emitted, "a headless app still took the widget path"
        assert emitted[0]["type"] == "user_input_requested"

        auq.resolve_over_rpc(a, emitted[0]["id"], "submit", [{"selected": [0]}])
        t.join(timeout=8)

    assert out and "SUBMITTED" in out[0]
