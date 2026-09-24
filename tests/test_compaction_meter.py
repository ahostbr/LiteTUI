"""The context meter moves when the conversation shrinks (T832).

🔴 MEASURED ON THE 35B, NOT REASONED ABOUT. A real `gui.conversations.compact`
against ninfer-serve published

    {"type":"compaction","reason":"compacted","tokens_before":6695,
     "tokens_after":2955,...}

and `gui.state.usage.context_tokens` read **6695** both before and after. The
`compacted` exit computed `tokens_after` for the event, swapped
`self.conversation`, and never touched `self.ctx_used` — so the footer's
context chip kept the pre-compaction figure, and its red/amber threshold with
it, until the NEXT turn's native usage happened to arrive.

    THE EVENT AND THE METER ARE TWO REPORTS OF ONE FACT, AND THEY DISAGREED BY
    3.5x. Ryan's own words on this feature were "why is it not compacting" — a
    chip still reading 86% straight after a compaction is that picture exactly.

ONE ASSIGNMENT, NOT THREE FIXES. `ctx_used` is a Textual reactive, and
`watch_ctx_used` fans out to every reader: the footer label
(`_refresh_ctx_label`), the host's `usage` event (T645) and the glassbox
`window_fill`. Patching the footer alone would have left the host's meter
stale, and three per-reader patches would be three ways to drift apart. The
colour is not asserted separately here for the same reason: it is a pure
function of `ctx_used / ctx_max` at app.py:4033-4037, so the number IS the
colour, and a second copy of those thresholds in this file would be a test and
an implementation sharing an enumeration.

⚠️ THE NUMBER IS AN ESTIMATE AND THE EVENT ALREADY SAYS SO
(`tokens_after_exact: False`). The exact total is unknowable until the next
request returns. That is why these arms assert the meter equals THE EVENT'S
figure rather than any number computed here.
"""

from __future__ import annotations

import pytest
from test_compaction_ui import (
    _Chunk,
    _run,
    _scripted_create,
    _seed,
    _settle,
    off_local_lm_studio,
)

from litetui import app as app_mod
from litetui.settings import Settings

#: 91.5% of the window — inside the band the footer paints bold red.
FULL = 30_000
WINDOW = 32_768


def _app(**overrides):
    """A mounted app whose window is nearly full and whose model is LOADED.

    `ctx_loaded` is not decoration: `ctx_max` is the model's CEILING until it
    loads, and every consumer of this reactive refuses to divide by a ceiling.
    """
    a = app_mod.LiteTUI()
    base = dict(clear_screen_after_compact=False, compact_keep_recent=2,
                wake_after_compact=False)
    base.update(overrides)
    a.settings = Settings(**base)
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    off_local_lm_studio(a)   # WS3 b5f1f40: see test_compaction_ui.off_local_lm_studio
    a._apply_context_length = lambda: None
    a.emitted = []
    a._rpc_emit = a.emitted.append
    a.ctx_max = WINDOW
    a.ctx_loaded = True
    a.ctx_used = FULL
    return a


def _compaction(a, reason="compacted"):
    events = [e for e in a.emitted
              if e.get("type") == "compaction" and e.get("reason") == reason]
    assert len(events) == 1, [e for e in a.emitted if e.get("type") == "compaction"]
    return events[0]


# ── the meter follows the event ──────────────────────────────────────────────


def test_the_meter_lands_on_the_number_the_event_published():
    """🔴 THE ARM FOR THE MEASURED DEFECT. Before this, the left side of this
    assertion stayed at 30,000 while the right said a few hundred."""
    async def body():
        create, _ = _scripted_create([[_Chunk(content="a summary")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            event = _compaction(a)
            assert a.ctx_used == event["tokens_after"]
            assert a.ctx_used < FULL, "the meter did not move at all"
    _run(body())


def test_the_host_is_told_the_new_occupancy_and_only_after_the_compaction():
    """🔴 THE OTHER READER, AND THE ORDER. A host (Frontier Chat) draws its ring
    from the `usage` event, so a stale `ctx_used` left ITS meter wrong too — the
    footer was never the only victim.

    Order is part of the contract: the compaction is published FIRST, so a
    sudden drop in occupancy always arrives with the event that accounts for
    it, never ahead of it."""
    async def body():
        create, _ = _scripted_create([[_Chunk(content="a summary")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            event = _compaction(a)
            usage = [e for e in a.emitted
                     if e.get("type") == "usage"
                     and e["usage"].get("context_tokens") == event["tokens_after"]]
            assert usage, [e for e in a.emitted if e.get("type") == "usage"]
            # BY IDENTITY, not by type. The window was seeded full, so a `usage`
            # carrying the PRE-compaction 30,000 legitimately precedes the
            # event; comparing the first `usage` of any kind would fail on a
            # correct ordering.
            assert a.emitted.index(event) < a.emitted.index(usage[0]), [
                (e.get("type"), e.get("reason") or e.get("usage"))
                for e in a.emitted]
    _run(body())


def test_the_footer_readout_stops_showing_the_old_size():
    """🔴 THE USER-VISIBLE HALF, asserted on the app's own rendered state rather
    than on a colour constant copied out of the implementation."""
    async def body():
        create, _ = _scripted_create([[_Chunk(content="a summary")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            before = a.ctx_label_text.plain
            assert "30,000" in before, before

            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            after = a.ctx_label_text.plain
            assert "30,000" not in after, after
            assert f"{a.ctx_used:,}" in after, (after, a.ctx_used)
    _run(body())


# ── the exits that changed nothing must move nothing ─────────────────────────


def test_a_conversation_that_is_already_recent_leaves_the_meter_alone():
    """🔴 THE NEGATIVE ARM THAT KEEPS THE FIX HONEST. `already_recent` did not
    touch the conversation, so the occupancy it reports is still the truth —
    and a decline that silently rewrote the meter to an estimate of an
    UNCHANGED conversation would be a new lie in place of the old one.

    This is the T827 shape: `_safe_tail` keeps all of a short body, `head` is
    empty, and there is nothing compaction is allowed to remove."""
    async def body():
        a = _app(compact_keep_recent=10)
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a._compact()
            await _settle(a, pilot)

            assert _compaction(a, "already_recent")["tokens_before"] == FULL
            assert a.ctx_used == FULL
    _run(body())


def test_a_conversation_too_short_to_compact_leaves_the_meter_alone():
    """⬜ The same rule at the other decline."""
    async def body():
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            a.conversation = [{"role": "system", "content": "sys"},
                              {"role": "user", "content": "hi"}]
            a._compact()
            await _settle(a, pilot)

            _compaction(a, "too_short")
            assert a.ctx_used == FULL
    _run(body())


def test_a_failed_compaction_leaves_the_meter_alone():
    """🔴 THE ONE THAT WOULD HURT MOST IF IT WERE WRONG. A failure changes
    nothing (`a failed compact must change nothing`, test_compaction_ui), so a
    meter that dropped anyway would report headroom that does not exist — and
    the next request would blow the window with the footer reading 1%."""
    async def body():
        async def boom(**kw):
            raise RuntimeError("engine said no")

        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            before = list(a.conversation)
            a.client.chat.completions.create = boom
            a._compact()
            await _settle(a, pilot)

            _compaction(a, "failed")
            assert a.conversation == before
            assert a.ctx_used == FULL
    _run(body())
