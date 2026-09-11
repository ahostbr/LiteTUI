"""T632 — Stop on a parked ask does nothing.

🔴 THE DEFECT, TWO CAUSES, EITHER SUFFICIENT ON ITS OWN.

1. `abort` borrowed the KEYBOARD's verb. It called `action_stop_turn`, whose
   first stop is a `ConfirmStop` dialog. T572's `refuse_over_rpc` correctly
   declines to open a keyboard dialog in a headless child — so the confirmation
   never happened and `_stop_requested` was never set. The SECOND abort could
   not help either: its hard-kill branch is gated on the very flag the first one
   failed to set. `abort` therefore had no path to stopping anything, with or
   without a question open.

2. A tool call parked in `_run_over_rpc` blocks a worker thread on a
   `threading.Event` whose only setters are an `answer` command and app
   shutdown. Stopping the turn does not touch it, and the thread cannot be
   cancelled from outside — `asyncio.to_thread` threads never can.

Meanwhile `abort` replied `{"stopped": true}` on every call, measured from
nothing. Host side (LiteSuite, orchestration_events 30313-30315) three
turn-interrupt-requested events went out and the turn stayed running.

    A CONTROL THAT CANNOT UNBLOCK THE THING IT IS FOR IS NOT A CONTROL — and
    one that REPORTS SUCCESS while doing nothing is worse than an error.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import ask_user_question as auq
from litetui import rpc as rpc_mod

ONE_QUESTION = {
    "questions": [
        {
            "label": "Approach",
            "question": "Which approach?",
            "options": [{"title": "A"}, {"title": "B"}],
        }
    ]
}


class FakeRpcApp:
    """A wire, a liveness flag, and the stop state `abort` now drives."""

    def __init__(self, chat_running: bool = True) -> None:
        self._rpc = True
        self.is_running = True
        self.emitted: list[dict] = []
        self._chat = chat_running
        self._stop_requested = False
        self._turn_abandoned = False
        self.forced = 0

    def _rpc_emit(self, data: dict) -> None:
        self.emitted.append(data)

    # --- the real app's contract, as `stop_turn_over_rpc` uses it -----------
    def _chat_running(self) -> bool:
        return self._chat

    def _force_stop(self) -> None:
        self.forced += 1
        self._stop_requested = False
        self._turn_abandoned = True

    def stop_turn_over_rpc(self) -> bool:
        # The real implementation lives on LiteTUI (app.py). Mirrored here with
        # the same branches so the rpc-level arms below drive the same shape;
        # `test_stop_over_rpc_needs_no_dialog` pins the real one.
        if not self._chat_running():
            return False
        if self._stop_requested:
            self._force_stop()
        else:
            self._stop_requested = True
        return True


def _ask_in_thread(app, args=ONE_QUESTION):
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(auq.run(args, app)), daemon=True)
    t.start()
    return t, out


def _wait_for_ask(app, timeout=3.0) -> dict:
    gate = threading.Event()
    for _ in range(int(timeout * 100)):
        for e in app.emitted:
            if e.get("type") == "user_input_requested":
                return e
        gate.wait(0.01)
    raise AssertionError("no user_input_requested was emitted")


def _join(t, out, timeout=8.0) -> str:
    t.join(timeout=timeout)
    assert not t.is_alive(), "the parked worker thread was never released"
    return out[0]


# ---------------------------------------------------------------------------
# the park, and what releases it
# ---------------------------------------------------------------------------

def test_abort_releases_a_thread_parked_in_an_ask():
    """🔴 The card. Before T632 this thread waited out the session."""
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    ask = _wait_for_ask(app)

    released = auq.cancel_pending_asks(app)

    assert released == [ask["id"]]
    text = _join(t, out)
    assert "ABORTED" in text


def test_the_returned_text_names_the_abort_and_not_an_Esc_nobody_pressed():
    """⚠️ The `cancel` text says "Ryan pressed Esc without answering".

    A turn the HOST stopped is a different event with a different actor, and the
    reader is a model that will reason from whichever one it is told. Reusing
    the Esc wording would have been the cheap fix and a false statement — the
    same defect class as T558's "stale request" message naming the wrong cause.
    """
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    _wait_for_ask(app)
    auq.cancel_pending_asks(app)
    text = _join(t, out)

    assert "ABORTED" in text
    assert "Esc" not in text
    assert "do not assume any option" in text


def test_a_second_abort_releases_nothing():
    """Idempotent — not a second write into a box nobody is reading."""
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    _wait_for_ask(app)

    first = auq.cancel_pending_asks(app)
    second = auq.cancel_pending_asks(app)

    assert len(first) == 1
    assert second == []
    _join(t, out)


def test_CONTROL_an_ANSWERED_ask_is_not_overwritten_by_a_later_abort():
    """🔴 Without this, `cancel_pending_asks` could resolve everything blindly.

    An abort arriving just after a real answer must not turn Ryan's submitted
    selection into "ABORTED — never put to Ryan". This is what the `is_set`
    check buys, and the only arm that can tell a guarded cancel from a
    scorched-earth one.
    """
    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    ask = _wait_for_ask(app)

    assert auq.resolve_over_rpc(app, ask["id"], "submit", [{"selected": [1], "note": "B"}])
    released = auq.cancel_pending_asks(app)

    assert released == [], "the abort clobbered an answer that had already landed"
    text = _join(t, out)
    assert "SUBMITTED" in text
    assert "ABORTED" not in text


# ---------------------------------------------------------------------------
# the rpc command itself
# ---------------------------------------------------------------------------

def test_abort_emits_a_resolution_for_every_ask_it_cancelled(monkeypatch):
    """The child SAYS what it did.

    ⬜ This is not what clears the host's card — LiteSuite's `interruptTurn`
    resolves its own pending requests locally, deliberately, because a child
    that is not reading its stdin cannot be relied on to answer. This is the
    child's RECORD that the question died rather than was answered, which is
    otherwise unrecoverable from either side.
    """
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    ask = _wait_for_ask(app)

    rpc_mod._dispatch(app, {"type": "abort", "id": "cmd_1"})

    resolved = [e for e in app.emitted if e.get("type") == "user_input_resolved"]
    assert [e["id"] for e in resolved] == [ask["id"]]
    assert resolved[0]["cancelled"] is True
    assert resolved[0]["reason"] == "abort"
    _join(t, out)


def test_the_abort_reply_reports_what_actually_happened(monkeypatch):
    """🔴 `stopped` is MEASURED now. It used to be the literal True, always.

    The host had no way to tell a stop that worked from one that did nothing,
    which is exactly the state Ryan was in: three interrupts sent, three
    successes reported, the turn still running.
    """
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    ask = _wait_for_ask(app)
    rpc_mod._dispatch(app, {"type": "abort", "id": "cmd_1"})
    _join(t, out)

    reply = next(r for r in replies if r.get("id") == "cmd_1")
    assert reply["ok"] is True
    assert reply["result"]["stopped"] is True
    assert reply["result"]["cancelled_asks"] == [ask["id"]]


def test_CONTROL_abort_with_nothing_running_reports_stopped_false(monkeypatch):
    """The other half of "measured": it must be able to say NO.

    An arm that only ever sees `stopped: true` cannot tell a measurement from
    the constant it replaced.
    """
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp(chat_running=False)
    rpc_mod._dispatch(app, {"type": "abort", "id": "cmd_9"})

    reply = next(r for r in replies if r.get("id") == "cmd_9")
    assert reply["result"]["stopped"] is False
    assert reply["result"]["cancelled_asks"] == []


# ---------------------------------------------------------------------------
# the app-level stop, which is the half `refuse_over_rpc` was silently eating
# ---------------------------------------------------------------------------

def test_stop_over_rpc_needs_no_dialog_and_escalates_like_Escape():
    """🔴 The REAL `LiteTUI.stop_turn_over_rpc`, not the double above.

    Driven on a bare instance so nothing can open a screen: if this method ever
    routes through `present_dialog` again, `_stop_requested` stays False here
    and this arm goes red — which is precisely how the defect survived.
    """
    from litetui.app import LiteTUI

    app = LiteTUI.__new__(LiteTUI)  # no __init__: no app, no loop, no screens
    app._rpc = True
    app._stop_requested = False
    app._turn_abandoned = False
    app._chat_running = lambda: True  # type: ignore[method-assign]
    forced: list[int] = []
    app._force_stop = lambda: forced.append(1)  # type: ignore[method-assign]

    assert app.stop_turn_over_rpc() is True
    assert app._stop_requested is True, "the first abort asked for nothing"
    assert forced == [], "the first abort should ask, not force"

    assert app.stop_turn_over_rpc() is True
    assert forced == [1], "the second abort did not escalate"


def test_stop_over_rpc_is_inert_with_no_turn_running():
    from litetui.app import LiteTUI

    app = LiteTUI.__new__(LiteTUI)
    app._rpc = True
    app._stop_requested = False
    app._chat_running = lambda: False  # type: ignore[method-assign]

    assert app.stop_turn_over_rpc() is False
    assert app._stop_requested is False
