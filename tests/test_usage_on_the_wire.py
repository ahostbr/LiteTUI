"""T645 — the child reports its context usage, so the host's meter can fill.

🔴 THE PANE SAYS "Context usage not reported yet" ON EVERY LITETUI THREAD. The
host derives the ring from a `thread.token-usage.updated` activity
(session-logic.ts:180 deriveThreadContextUsage); the codex adapter emits one and
this child emitted nothing. The numbers existed the whole time — `ctx_used`,
`ctx_max`, `last_usage` — they simply never left the process.

⬜ THE HOST'S KEYS ARE ALREADY PERMISSIVE, so the wire shape is chosen to match
what it reads rather than inventing one: `findNumberDeep` accepts
`context_tokens` among its used-token spellings and `max_context_tokens` among
its max spellings (session-logic.ts:206-230).

🔴 AND `max_context_tokens` IS WITHHELD UNLESS `ctx_loaded`, WHICH IS THE WHOLE
CARE IN THIS CARD. `ctx_max`'s own comment says it may be the model's CEILING
rather than the LOADED window — the qwen 262,144-vs-8k lesson — and "anything
that divides by ctx_max must check this first". The ring divides. Sending a
ceiling would draw a confident, wrong, mostly-empty meter; sending no max at all
makes the host return null and the pane keeps saying "not reported yet", which
is what it says today and is TRUE. A wrong meter is worse than an honest blank.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as app_mod


def _app(*, used=None, mx=None, loaded=False, rpc=True, last_usage=None):
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a._rpc = rpc
    a.ctx_max = mx
    a.ctx_loaded = loaded
    a.last_usage = last_usage
    a.emitted: list[dict] = []
    a._rpc_emit = a.emitted.append
    a._usage_payload_used = used
    return a


def _payload(a, used):
    """The usage event for a given ctx_used, or None when nothing was emitted."""
    app_mod.LiteTUI._rpc_emit_usage(a, used)
    events = [e for e in a.emitted if e.get("type") == "usage"]
    return events[-1]["usage"] if events else None


def test_it_reports_the_used_tokens_under_a_key_the_host_reads():
    assert _payload(_app(mx=32768, loaded=True), 1234)["context_tokens"] == 1234


def test_it_reports_the_window_when_the_window_is_KNOWN_to_be_loaded():
    assert _payload(_app(mx=32768, loaded=True), 1234)["max_context_tokens"] == 32768


def test_it_WITHHOLDS_the_window_when_ctx_max_is_only_a_ceiling():
    """🔴 THE CARE IN THIS CARD.

    ctx_loaded False means ctx_max is the model's advertised maximum, not what
    LM Studio actually loaded. Dividing by it would draw a meter that is wrong
    by whatever factor those differ — 262144 vs 8192 in the case the app's own
    docstring records.
    """
    payload = _payload(_app(mx=262144, loaded=False), 1234)

    assert payload["context_tokens"] == 1234
    assert "max_context_tokens" not in payload


def test_the_host_treats_a_missing_window_as_NOT_REPORTED():
    """
    Pinning the consequence, not just the omission: deriveThreadContextUsage
    returns null when maxTokens is null or <= 0, so the pane keeps its honest
    "Context usage not reported yet" instead of a wrong ring. This arm documents
    the contract this file depends on; the host side asserts it for real.
    """
    payload = _payload(_app(mx=None, loaded=True), 999)

    assert payload is not None and "max_context_tokens" not in payload


def test_provider_token_counts_ride_along_when_there_are_some():
    """`last_usage` is the provider's own accounting (T624). Extra keys are
    harmless to the host — findNumberDeep looks for the ones it knows."""
    payload = _payload(
        _app(mx=32768, loaded=True, last_usage={"prompt_tokens": 100, "completion_tokens": 7}),
        1234,
    )

    assert payload["prompt_tokens"] == 100
    assert payload["completion_tokens"] == 7


def test_a_None_last_usage_does_not_become_junk_on_the_wire():
    payload = _payload(_app(mx=32768, loaded=True, last_usage=None), 1234)

    assert payload == {"context_tokens": 1234, "max_context_tokens": 32768}


def test_nothing_is_emitted_when_there_is_no_number_to_report():
    """🔴 An empty meter and an unknown meter are different claims.

    An empty meter and an unknown meter are different claims. Before the first
    turn `ctx_used` is None, and reporting 0 would draw a window the user has
    not filled rather than one nobody has measured.
    """
    assert _payload(_app(mx=32768, loaded=True), None) is None


def test_CONTROL_an_interactive_session_emits_nothing_at_all():
    """`_rpc_emit` no-ops outside --rpc; this proves the guard rather than
    assuming it, since the same reactive fires in the TUI on every turn."""
    a = _app(mx=32768, loaded=True, rpc=False)
    a._rpc_emit = lambda data: a.emitted.append(data) if a._rpc else None

    app_mod.LiteTUI._rpc_emit_usage(a, 1234)

    assert a.emitted == []
