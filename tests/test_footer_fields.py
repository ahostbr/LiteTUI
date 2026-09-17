"""The footer shows a context percent, and every field can be hidden.

The percentage was ALREADY being computed — `pct` picks the colour thresholds —
and then discarded, so the footer knew how full the window was and made you do
the division yourself.

🔴 EVERY PILOT HERE RUNS AT `WIDE`, AND THAT IS LOAD BEARING (T816).
`run_test()` defaults to 80 columns. `ctx_label_text` is WIDTH-AWARE: when the
footer knows its width it drops `("tps", "convo", "bg", "agents", "think",
"ctx")` in that order until the rest fits, protecting authority/plan/seat/pct
(`28c39a2 fix(footer): preserve identity and context percent when narrow`).
Measured at the default size:

    (80, 24)  -> '>> autonomous on  ·  plan:off  ·  unregistered  ·  50%'
    (200, 24) -> '... think:medium · 616cc9c0 · ctx 60,000 / 120,000 max · 50% · 70.7 tok/s'

So six arms in this file were red for a reason that had NOTHING to do with what
they test: at 80 columns the field was dropped for WIDTH, not hidden by its
SETTING, and those two causes are indistinguishable from an assertion that only
looks for absence.

    AN ARM THAT CANNOT TELL "HIDDEN BY ITS SWITCH" FROM "DROPPED FOR WIDTH" IS
    NOT TESTING THE SWITCH.

⬜ NO COVERAGE IS LOST, and that was checked rather than assumed: the drop
policy has its own home in `tests/test_footer.py`, whose FakeApp sets
`_footer_available_width` explicitly (`:63`) and asserts the narrow case. This
file tests the settings; that file tests the width.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-footer-"))


#: Wide enough that the width policy drops NOTHING, so a missing field can only
#: mean its setting hid it. Not a round number for its own sake: at 80 columns
#: (the `run_test()` default) every droppable field is gone.
WIDE = (200, 24)


def _app(**overrides):
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    a.settings = Settings(**overrides)
    a.ctx_used, a.ctx_max, a.tps = 23_133, 120_064, 70.7
    a.convo_id = "616cc9c0-dead-beef-cafe"
    a.thinking_level = "medium"
    return a


@pytest.mark.asyncio
async def test_the_percent_is_shown():
    a = _app()
    async with a.run_test(size=WIDE) as pilot:
        await pilot.pause()
        # 23133 / 120064 = 19.27%
        assert "19%" in a.ctx_label_text.plain


@pytest.mark.asyncio
async def test_the_percent_agrees_with_the_counts():
    """A ratio that disagrees with the numbers beside it is worse than neither."""
    a = _app()
    a.ctx_used, a.ctx_max = 60_000, 120_000
    async with a.run_test(size=WIDE) as pilot:
        await pilot.pause()
        text = a.ctx_label_text.plain
        assert "60,000 / 120,000" in text
        assert "50%" in text


@pytest.mark.parametrize(
    "flag,needle",
    [
        ("footer_show_thinking", "think:"),
        ("footer_show_convo", "616cc9c0"),
        ("footer_show_context", "ctx "),
        ("footer_show_context_pct", "19%"),
        ("footer_show_tps", "tok/s"),
    ],
)
@pytest.mark.asyncio
async def test_each_field_can_be_hidden(flag, needle):
    """Every toggle must actually remove its field — a switch that renders and
    changes nothing is the failure this whole settings effort exists to stop."""
    on = _app()
    off = _app(**{flag: False})
    async with on.run_test(size=WIDE) as pilot:
        await pilot.pause()
        assert needle in on.ctx_label_text.plain, f"{needle!r} missing while {flag} is ON"
    async with off.run_test(size=WIDE) as pilot:
        await pilot.pause()
        assert needle not in off.ctx_label_text.plain, f"{flag}=False did not hide {needle!r}"


@pytest.mark.asyncio
async def test_the_percent_survives_hiding_the_raw_counts():
    """The ratio is usually the only part worth reading, so it must stand alone."""
    a = _app(footer_show_context=False)
    async with a.run_test(size=WIDE) as pilot:
        await pilot.pause()
        text = a.ctx_label_text.plain
        assert "19%" in text
        assert "23,133" not in text


@pytest.mark.asyncio
async def test_separators_do_not_survive_a_hidden_field():
    """Hiding a field must not leave its separator behind.

    A footer reading "  ·    ·  19%" tells you something is broken rather than
    hidden, which is a different message from the one intended.
    """
    a = _app(
        footer_show_seat=False,
        footer_show_thinking=False,
        footer_show_convo=False,
        footer_show_context=False,
    )
    async with a.run_test(size=WIDE) as pilot:
        await pilot.pause()
        text = a.ctx_label_text.plain
        assert not text.startswith(" "), f"leading separator: {text!r}"
        assert "·  ·" not in text, f"doubled separator: {text!r}"


@pytest.mark.asyncio
async def test_tok_per_second_survives_an_unresolved_context_window():
    """REGRESSION. The old body returned EARLY when used and max were both None:

        if used is None and mx is None:
            t.append("ctx —", "dim")
            return t          # <- before _append_tps

    so tok/s was unreachable whenever the window had not resolved. Fields are
    appended in order now, with one return, so no field can suppress a later one.
    """
    a = _app()
    a.ctx_used = a.ctx_max = None
    async with a.run_test(size=WIDE) as pilot:
        await pilot.pause()
        text = a.ctx_label_text.plain
        assert "ctx" in text
        assert "tok/s" in text, f"tok/s was suppressed by an unresolved window: {text!r}"
