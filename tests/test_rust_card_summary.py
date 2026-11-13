"""native_card_summary: a reply card's title reaches the native frontend, and only it."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from litetui import native_card_summary


def app(rpc=True, listening=True):
    sent = []
    return SimpleNamespace(_rpc=rpc, _gui_system_lines_open=listening, _rpc_emit=sent.append, sent=sent)


def test_a_summary_goes_out_naming_its_answer_by_digest():
    a = app()
    card = SimpleNamespace(summary="Explains the fix", answer_text="  The answer.\n", thinking=None)
    native_card_summary.publish(a, card)
    assert a.sent == [{"type": "card_summary", "version": 1,
                       "sha256": hashlib.sha256(b"The answer.").hexdigest(),
                       "thinking": False, "summary": "Explains the fix"}]


def test_a_thinking_only_card_is_named_by_its_trace():
    a = app()
    card = SimpleNamespace(summary="thinking: Plan first.", answer_text="",
                           thinking=SimpleNamespace(source="Plan first. Then build."))
    native_card_summary.publish(a, card)
    assert a.sent[0]["thinking"] is True
    assert a.sent[0]["sha256"] == hashlib.sha256(b"Plan first. Then build.").hexdigest()


def test_nothing_goes_out_without_a_listening_native_frontend_or_a_summary():
    card = SimpleNamespace(summary="S", answer_text="A", thinking=None)
    for a in (app(rpc=False), app(listening=False)):
        native_card_summary.publish(a, card)
        assert a.sent == [] and not native_card_summary.wanted(a)
    a = app()
    native_card_summary.publish(a, SimpleNamespace(summary=None, answer_text="A", thinking=None))
    assert a.sent == []


def test_the_rpc_settle_kicks_the_summary_only_for_a_listening_frontend():
    import inspect

    from litetui.app import LiteTUI
    source = inspect.getsource(LiteTUI._settle_turn_stop_line)
    rpc = source[source.index("if self._rpc:"):source.index("if getattr(self, \"_turn_stop_line_settled\"")]
    assert "native_card_summary.wanted(self)" in rpc and "self._kick_card_summary(widget)" in rpc
    summarise = inspect.getsource(LiteTUI._summarise_card)
    assert summarise.index("card.set_summary(text)") < summarise.index("native_card_summary.publish(self, card)")
