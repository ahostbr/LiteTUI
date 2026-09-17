"""Every header write survives a block that is not composed yet (T833).

🔴 MEASURED, NOT IMAGINED. Seven minutes against the 35B-A3B — a reasoning
model, so every turn builds one of these — left SIX of these in the child's
stderr:

    app.py:4717 _elapsed_repaint -> widgets.py:448 repaint_header
    textual.css.query.NoMatches: No nodes match 'ThinkingHeader' on
    ThinkingBlock(classes='expanded thinking-block')
    Task exception was never retrieved

A ThinkingBlock stamps `_t0` in `__init__`, but Textual does not compose its
children until it is mounted. `repaint_header` runs from a 0.25 s timer, so
anything reached inside that gap throws — into a task nobody awaits.

⬜ THE ROOT CAUSE IS NOT THE MISSING try. `reset_header` and `freeze_header`
each already carried one, and `reset_header`'s comment names the mechanism in
full: "a fast stream can end the trace before the block's children exist."

    A COMMENT THAT STATES A RULE DOES NOT TRANSFER TO THE SIBLING IT NEVER
    NAMES. Two of the four writers had it and two did not — and `set_expanded`
    was the second gap, which reading only the two the report named would have
    missed.

So the arms below go through EVERY writer, and the last one pins the structural
fact: there is one `query_one(ThinkingHeader)` left in the module, so a fifth
writer cannot be added without meeting the guard.
"""

from __future__ import annotations

import inspect

from litetui import widgets


def _uncomposed():
    """A block in exactly the state the timer catches: constructed, `_t0`
    stamped by `__init__`, no children — never mounted."""
    block = widgets.ThinkingBlock()
    assert block._t0 is not None, "the fixture must reproduce a LIVE trace"
    return block


# ── every writer, in the gap ─────────────────────────────────────────────────


def test_a_repaint_tick_before_compose_does_not_throw():
    """🔴 THE ARM FOR THE SIX TRACEBACKS. This is the call app.py's
    `_elapsed_repaint` makes, on the block state it can catch."""
    _uncomposed().repaint_header(412.5, 91)


def test_a_reset_before_compose_does_not_throw():
    """⬜ The writer that already had a guard — it must still have one after the
    four were merged into a single door."""
    _uncomposed().reset_header()


def test_a_freeze_before_compose_does_not_throw_and_keeps_its_numbers():
    """⬜ The other pre-existing guard, plus the thing its comment promises:
    the readout is kept either way, so a later toggle can paint it."""
    block = _uncomposed()
    # Set directly: append() paints through the live app and this block has
    # none. The arm is about the HEADER write, not about buffering.
    block._toks = 7
    block.freeze_header()
    assert block._frozen is not None, "the numbers were dropped with the paint"
    assert block._t0 is None, "the timer was left running"


def test_a_toggle_before_compose_does_not_throw():
    """🔴 THE WRITER NEITHER THE REPORT NOR THE CARD NAMED. `set_expanded` wrote
    the header unguarded too; it is reached from a click, so it is far less
    likely to lose the race — which is exactly why it would have been left."""
    block = _uncomposed()
    block.set_expanded(False)
    assert not block.expanded
    block.set_expanded(True)
    assert block.expanded


# ── the structure that keeps it true ─────────────────────────────────────────


def test_there_is_exactly_one_door_to_the_header():
    """🔴 THE ARM AGAINST THE NEXT SIBLING. Four separate writers is how two of
    them ended up without the rule; a fifth added later must come through
    `_set_header` or this goes red.

    Counted on the module SOURCE rather than by calling each writer, because
    the failure this prevents is a write that no test knows to call yet."""
    source = inspect.getsource(widgets)
    assert source.count("query_one(ThinkingHeader)") == 1, (
        "a header write bypassing _set_header has been added")


def test_the_guard_swallows_only_the_paint_and_not_the_state():
    """⬜ THE COST OF A CATCH-ALL, BOUNDED. `_set_header` deliberately keeps the
    broad `except Exception` the two original guards used, so this pins what it
    is allowed to hide: the write, and nothing the callers did first."""
    block = _uncomposed()
    block._toks = 2
    block._set_header("anything at all")
    assert block._toks == 2, "the guard ate state that was already recorded"
