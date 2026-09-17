"""The compaction must SAY what it did, on the wire (T825).

🔴 IT WAS INVISIBLE TO EVERY HOST. `_compact` reported itself with `_system`
lines and nothing else, so LiteSuite's Frontier Chat could show Ryan neither a
compaction, nor a decline, nor a failure — while his own question was
"why is it not compacting".

⬜ I MET THAT BLINDNESS AS A MEASURER BEFORE IT WAS A CARD. Four driver runs
waited for a `turn_end` after the compaction; `_compact` is not `_stream` and
emits none, so their silence read as "autocompact did not fire" when it is
silence either way.

    AN INSTRUMENT SILENT ON BOTH OUTCOMES CANNOT REPORT EITHER. That is the
    whole reason a DECLINE emits too, and not only a success.

TWO EXACTNESS FLAGS, NOT ONE (ruling 0dcd7777): `tokens_before` is the engine's
own count, `tokens_after` can only ever be a chars/4 estimate here — the real
total is not known until the NEXT request returns. One bool over two numbers of
different provenance would mislabel one or drop the qualifier from both.
"""

from __future__ import annotations

import pytest

from litetui import app as app_mod

FIELDS = {
    "type", "reason", "tokens_before", "tokens_after",
    "tokens_before_exact", "tokens_after_exact",
    "messages_dropped", "messages_summarised", "kept_recent",
}


def _app():
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.emitted = []
    a._rpc_emit = a.emitted.append
    return a


def _only(a):
    events = [e for e in a.emitted if e.get("type") == "compaction"]
    assert len(events) == 1, a.emitted
    return events[0]


# ── the contract NeonRack's adapter maps ─────────────────────────────────────


def test_every_field_the_adapter_maps_is_present_on_every_event():
    """🔴 THE ARM FOR THE WHOLE CLASS. A missing key is `undefined` on the other
    side of the wire, and the notice silently renders less rather than failing —
    so an omission here is invisible from over there."""
    a = _app()
    for reason in ("compacted", "already_recent", "too_short", "failed",
                   "deferred_busy"):
        a.emitted.clear()
        a._emit_compaction(reason)
        assert set(_only(a)) == FIELDS, reason


def test_the_two_exactness_flags_are_independent():
    """🔴 THE RULING, PINNED. A single bool cannot describe an engine count and
    an estimate in the same object."""
    a = _app()
    a._emit_compaction("compacted", tokens_before=27961, tokens_after=7855,
                       tokens_before_exact=True, tokens_after_exact=False)
    e = _only(a)
    assert (e["tokens_before"], e["tokens_before_exact"]) == (27961, True)
    assert (e["tokens_after"], e["tokens_after_exact"]) == (7855, False)


def test_the_flags_are_always_real_bools():
    """⬜ `ctx_used is not None` is passed at the call sites; a host branching on
    truthiness must not receive None."""
    a = _app()
    a._emit_compaction("failed", tokens_before=None,
                       tokens_before_exact=None, tokens_after_exact=None)
    e = _only(a)
    assert e["tokens_before_exact"] is False
    assert e["tokens_after_exact"] is False


# ── the counts ───────────────────────────────────────────────────────────────


def test_summarised_is_sent_not_derived():
    """🔴 NeonRack REFUSED TO DERIVE IT, AND HE IS RIGHT. A failed compact drops
    nothing and summarises nothing; deriving `messages_summarised` from
    `messages_dropped` would dress a failure as a completed compaction. 0 is a
    real answer and arrives as one.

    🔴 AND THE FIRST VERSION OF THIS ARM COULD NOT SEE THAT. It asserted
    (0, 0) and then (10, 10) — two cases where dropped and summarised are
    EQUAL, so a derivation satisfies both. The mutation survived, and the
    docstring above it claimed the opposite.

        AN ASSERTION SATISFIABLE BY THE WRONG ANSWER IS NOT A TEST OF THE
        DISTINCTION IT NAMES. The pair must DIFFER.
    """
    a = _app()
    # The shape NeonRack named: messages went, no summary came back.
    a._emit_compaction("compacted", messages_dropped=10, messages_summarised=0)
    e = _only(a)
    assert e["messages_dropped"] == 10
    assert e["messages_summarised"] == 0, "derived from messages_dropped"

    a.emitted.clear()
    a._emit_compaction("failed")
    e = _only(a)
    assert (e["messages_dropped"], e["messages_summarised"]) == (0, 0)

    a.emitted.clear()
    a._emit_compaction("compacted", messages_dropped=10, messages_summarised=10)
    assert _only(a)["messages_summarised"] == 10


# ── the five exits, through the real _compact ────────────────────────────────


def _conversation_app(messages, keep_recent=4):
    """A REAL LiteTUI, not `__new__`.

    `ctx_used` is a Textual reactive and setting one on an instance that never
    ran `__init__` raises ReactiveError — the pure-emitter arms above get away
    with `__new__` because they touch no reactive at all.
    """
    from litetui.settings import Settings

    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    a.conversation = list(messages)
    a.settings = Settings(compact_keep_recent=keep_recent)
    a.ctx_used = 28190
    a.emitted = []
    a._rpc_emit = a.emitted.append
    a.said = []
    a._system = lambda m, *x, **k: a.said.append(str(m))
    a._compact_is_auto = False
    return a


@pytest.mark.asyncio
async def test_too_short_declines_audibly():
    a = _conversation_app([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
    ])
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # `_compact` is @work, so calling it SCHEDULES and returns a Worker —
        # awaiting the return value is a TypeError. Drive it the way the repo's
        # own turn tests drive `_stream`: schedule, then settle.
        a._compact()
        for _ in range(200):
            await pilot.pause()
            if not a._chat_running():
                break
    e = _only(a)
    assert e["reason"] == "too_short"
    assert any("have a conversation first" in s for s in a.said)


@pytest.mark.asyncio
async def test_already_recent_declines_audibly_and_names_the_window():
    """🔴 THE STATE THAT COST FOUR RUNS. The window was at 86% and could not be
    reduced, because everything in it was recent — and nothing on the wire said
    so. See T827 for the pre-flight that should stop the shape arising."""
    a = _conversation_app([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
        {"role": "assistant", "content": "four"},
    ])
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # `_compact` is @work, so calling it SCHEDULES and returns a Worker —
        # awaiting the return value is a TypeError. Drive it the way the repo's
        # own turn tests drive `_stream`: schedule, then settle.
        a._compact()
        for _ in range(200):
            await pilot.pause()
            if not a._chat_running():
                break
    e = _only(a)
    assert e["reason"] == "already_recent"
    assert e["tokens_before"] == 28190
    assert e["tokens_before_exact"] is True
    assert e["kept_recent"] == 4
    assert any("already recent" in s for s in a.said)
