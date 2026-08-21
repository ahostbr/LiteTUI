"""Mid-turn input is HELD and flushed, never inert-injected or accidentally
cancelling.

The two bugs this replaces, both watched live 2026-08-21:
  1. A user message mid-turn CANCELLED the turn — nobody wrote an interrupt,
     it was a side effect of @work(exclusive=True, group="chat").
  2. Inbox mail mid-turn was appended straight into the conversation, where it
     sat INERT for four turns: nothing announced it, and the model's own inbox
     tool truthfully said "(no new messages)" because the monitor had already
     claimed the mail — so the model trusted the tool over its own context.

Ryan's spec, verbatim clause included: queue by default; ctrl+shift+enter
interrupts; and "swapping the default behavior between those two in the
settings page" — the two ENDS trade places, not a hardcoded chord.
"""
from pathlib import Path
from types import SimpleNamespace

from textual.worker import WorkerState

from app import LiteTUI, midturn_action

APP_SRC = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")


# --- the mapping: ONE swap, both ways round ----------------------------------
def test_default_enter_queues_and_chord_interrupts():
    assert midturn_action(enter_interrupts=False, alt_chord=False) == "queue"
    assert midturn_action(enter_interrupts=False, alt_chord=True) == "interrupt"


def test_swapped_enter_interrupts_and_chord_queues():
    """THE CLAUSE THE FIRST DISPATCH LOST. Under the swapped setting the chord
    must QUEUE — a hardcoded interrupt chord leaves no way to queue at all."""
    assert midturn_action(enter_interrupts=True, alt_chord=False) == "interrupt"
    assert midturn_action(enter_interrupts=True, alt_chord=True) == "queue"


# --- flush machinery ---------------------------------------------------------
flush = LiteTUI._flush_pending_input


class FlushDouble(SimpleNamespace):
    def __init__(self, pending, running=False):
        super().__init__(
            _pending_input=list(pending),
            _running=running,
            appended=[], streamed=0, timers=[], materialised=0,
        )
        # the real code passes this method by reference to set_timer
        self._flush_pending_input = lambda: None

    def _chat_running(self):
        return self._running

    def set_timer(self, delay, cb):
        self.timers.append((delay, cb))

    def _materialise_convo(self):
        self.materialised += 1

    def _append(self, m):
        self.appended.append(m)

    def _stream(self):
        self.streamed += 1


def test_flush_is_a_no_op_with_nothing_pending():
    d = FlushDouble([])
    flush(d)
    assert d.streamed == 0 and d.timers == []


def test_flush_retries_rather_than_racing_a_running_group():
    """_compact shares the chat group; a flush that streamed now would CANCEL
    it. The retry is the difference between queueing and a new cancel bug."""
    d = FlushDouble([{"content": "hi", "text": "hi"}], running=True)
    flush(d)
    assert d.streamed == 0
    assert len(d.timers) == 1
    assert d._pending_input          # nothing consumed while waiting


def test_flush_sends_exactly_one_fifo_message_per_turn_end():
    """Consecutive role:user messages are a chat-template gamble (qwen's
    template 500s on some shapes) — each held message gets its own turn."""
    d = FlushDouble([{"content": "first", "text": "first"},
                     {"content": "second", "text": "second"}])
    flush(d)
    assert [m["content"] for m in d.appended] == ["first"]
    assert d.streamed == 1
    assert [i["content"] for i in d._pending_input] == ["second"]


# --- the single flush point --------------------------------------------------
def test_every_chat_worker_ending_reaches_the_flush():
    """on_worker_state_changed is the ONE site: exits multiply, and a flush
    call at each of _stream's returns is a list someone forgets to extend."""
    calls = []
    ns = SimpleNamespace(call_after_refresh=lambda cb: calls.append(cb),
                         _flush_pending_input=lambda: None)
    for state in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
        ev = SimpleNamespace(worker=SimpleNamespace(group="chat"), state=state)
        LiteTUI.on_worker_state_changed(ns, ev)
    assert len(calls) == 3
    # negative arm: other groups never flush
    ev = SimpleNamespace(worker=SimpleNamespace(group="inbox"), state=WorkerState.SUCCESS)
    LiteTUI.on_worker_state_changed(ns, ev)
    assert len(calls) == 3


# --- inbox mail through the same path ---------------------------------------
deliver = LiteTUI._deliver_inbox


class InboxDouble(SimpleNamespace):
    def __init__(self, running):
        super().__init__(_running=running, _pending_input=[], bubbles=[],
                         appended=[], streamed=0)

    def _chat_running(self):
        return self._running

    def _user_bubble(self, text, has_image, queued=False):
        self.bubbles.append((text[:20], queued))

    def _append(self, m):
        self.appended.append(m)

    def _stream(self):
        self.streamed += 1


def _fake_msg():
    return {"from": "abc12345", "priority": "normal", "body": "hello"}


def test_inbox_mail_midturn_is_held_not_appended():
    """THE INERT-INJECTION FIX. Appending mid-turn puts the mail where nothing
    announces it; holding it makes it a real turn the model cannot miss."""
    d = InboxDouble(running=True)
    deliver(d, _fake_msg())
    assert d.appended == []          # NOT in the conversation yet
    assert d.streamed == 0           # and nothing cancelled
    assert len(d._pending_input) == 1
    assert d.bubbles and d.bubbles[0][1] is True   # visibly queued


def test_inbox_mail_idle_sends_immediately():
    d = InboxDouble(running=False)
    deliver(d, _fake_msg())
    assert len(d.appended) == 1 and d.streamed == 1
    assert d._pending_input == []


# --- source gates ------------------------------------------------------------
def test_the_chord_binding_exists_with_priority():
    assert '"ctrl+shift+enter", "submit_alt"' in APP_SRC
    line = next(l for l in APP_SRC.split(chr(10)) if '"ctrl+shift+enter"' in l)
    assert "priority=True" in line, "Input would swallow the chord without priority"


def test_the_measured_ctrl_j_alias_exists():
    """MEASURED on the real terminal (keyprobe, 2026-08-21): Windows Terminal
    ignores the kitty protocol, so ctrl+enter AND ctrl+shift+enter both arrive
    as 'ctrl+j'. Without this alias the chord binding above is handled-and-
    does-nothing on this box — the user presses the chord and nothing fires."""
    assert '"ctrl+j", "submit_alt"' in APP_SRC


def test_wake_after_compact_defers_to_a_real_pending_message():
    body = APP_SRC.split("def _wake_after_compact", 1)[1]
    body = body.split(chr(10) + "    @", 1)[0]
    assert "_pending_input" in body
