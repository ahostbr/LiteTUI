"""A reply card's one-line summary, for a native frontend.

Python titles each settled assistant card "<summary> - <model>" (AssistantMessage
._header_text): the card-summary side call (_kick_card_summary / _summarise_card), a recap,
or the first sentence of a thinking-only card. Over RPC the turn's stop line is the
frontend's, so _settle_turn_stop_line returned before kicking the summary and a native
frontend's cards kept the bare model name. For a frontend that asked for Python's chat-log
lines (the Rust frontend, native_system_lines) the summary now runs as it does on screen,
and each one goes out as a ``card_summary`` event naming the answer it describes by digest.
"""
from __future__ import annotations

import hashlib

EVENT = "card_summary"
VERSION = 1
MAX_SUMMARY = 1024


def wanted(app) -> bool:
    """A native frontend that draws its own cards is listening."""
    return bool(getattr(app, "_rpc", False) and getattr(app, "_gui_system_lines_open", False))


def digest(answer: str) -> str:
    return hashlib.sha256((answer or "").strip().encode("utf-8")).hexdigest()


def publish(app, card) -> None:
    """Send `card`'s summary, if it has one and a native frontend is listening."""
    summary = getattr(card, "summary", None)
    if not wanted(app) or not summary:
        return
    answer = getattr(card, "answer_text", "") or ""
    # A thinking-only card is titled from its trace, so the trace is what names it.
    thinking = not answer.strip()
    basis = (getattr(getattr(card, "thinking", None), "source", "") or "") if thinking else answer
    app._rpc_emit({"type": EVENT, "version": VERSION, "sha256": digest(basis),
                   "thinking": thinking, "summary": str(summary)[:MAX_SUMMARY]})
