"""The palette knows the app's features, and the header stops lying.

Two claims, both about derivation:
  - every palette row that routes through _handle_command names a command the
    DISPATCHER actually handles (the drift gate — a renamed command breaks a
    test, not a palette row), and
  - the header's tool count is COMPUTED from _all_tools(), the list the model
    is offered, never a literal ("tools:4" was a literal, and it stayed 4
    while seven more tools arrived).
"""

from __future__ import annotations

import asyncio
import sys
from functools import partial
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from textual.widgets import Select
from litetui.plugins.scheduler_ui import CalendarScreen, DayScreen, JobScreen
from litetui.widgets import ContextFooter, PaletteButton
from textual.command import CommandPalette
from litetui import paths
from litetui import scheduler as sched_mod

APP_SRC = (Path(__file__).resolve().parent.parent / "src" / "litetui" / "app.py").read_text(
    encoding="utf-8", errors="ignore"
)


@pytest.fixture(autouse=True)
def _never_write_the_live_jobs_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = []
    return a


def _run(coro):
    return asyncio.run(coro)


def _table(app):
    """The provider's table, with the group prefix stripped off the title.

    Rows render as "Backend  >  Switch model" since the palette was grouped
    (2026-08-22) -- Textual has no section headers, so the group name leads the
    title. Every assertion in this file is about the ROW: that it exists, and
    that running it reaches the right handler. Matching the decorated label
    instead would make each of them a test of the prefix, which is already
    covered by tests/test_palette_groups.py.
    """
    provider = m.LiteTUICommands(app.screen)
    return [
        (title.split("›")[-1].strip(), help_text, run)
        for title, help_text, run in provider._commands()
    ]


# --------------------------------------------------------------------------
# registration and coverage
# --------------------------------------------------------------------------

def test_the_provider_is_registered_on_the_app():
    assert m.LiteTUICommands in m.LiteTUI.COMMANDS, (
        "a Provider class nobody registers is not in any palette"
    )
    # And the stock providers survive — extending must not replace.
    assert len(m.LiteTUI.COMMANDS) > 1


def test_the_palette_covers_the_features_it_was_missing():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            titles = {t for t, _h, _r in _table(a)}
            missing = {
                "Calendar", "New scheduled job", "Scheduled jobs", "Settings",
                "Switch model", "New conversation", "Conversations",
                "Compact conversation", "Skills", "Mark the screen",
                "Tools", "Help",
            } - titles
            assert not missing, f"palette rows absent: {sorted(missing)}"
    _run(body())


def test_the_palette_is_a_complete_derivation_of_the_registry():
    """The successor to the old drift gate, HONESTLY scoped.

    Derivation made row-token drift structurally impossible: a derived row
    takes its token FROM the registry, so token-in-registry is a tautology
    (proven by mutation — renaming /calendar passed the old form). What CAN
    still fail is the derivation itself: an entry with palette metadata that
    never becomes a row, a row duplicated per alias, or an escape-hatch row
    lost. So the gate counts BOTH SIDES from independent walks."""
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            rows = _table(a)
            titles = [t for t, _h, _r in rows]
            assert len(titles) == len(set(titles)), f"duplicated rows: {titles}"
            palette_entries = {id(e): e.palette for e in a.plugins.commands.values()
                               if e.palette is not None}
            for want in palette_entries.values():
                assert want in titles, f"registered palette {want!r} never became a row"
            hatch = [r.title for r in a.plugins.palette_rows]
            for want in hatch:
                assert want in titles, f"escape-hatch row {want!r} lost in derivation"
            assert len(rows) == len(palette_entries) + len(hatch), (
                f"{len(rows)} rows != {len(palette_entries)} palette commands "
                f"+ {len(hatch)} escape hatches"
            )
    _run(body())


def test_running_scheduled_jobs_row_reaches_the_cron_list(tmp_path, monkeypatch):
    """The one escape hatch whose embedded command string ("/cron list")
    nothing else exercises — a dead token here is now the ONLY way a palette
    row can rot, so it is run for real."""
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            msgs = []
            a._system = lambda t: msgs.append(t)
            row = [r for t, _h, r in _table(a) if t == "Scheduled jobs"]
            assert row, "Scheduled jobs row missing"
            row[0]()
            await pilot.pause()
            assert msgs and "job" in msgs[-1].lower(), (
                f"the /cron list embedded in the row did not reach cron: {msgs}"
            )
    _run(body())


# --------------------------------------------------------------------------
# search and execution
# --------------------------------------------------------------------------

def test_search_finds_the_calendar_and_scores_it():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            provider = m.LiteTUICommands(a.screen)
            hits = [h async for h in provider.search("calend")]
            assert hits, "searching 'calend' found nothing"
            texts = [str(h.match_display) for h in hits]
            assert any("Calendar" in t for t in texts)
    _run(body())


def test_search_for_nonsense_finds_nothing():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            provider = m.LiteTUICommands(a.screen)
            hits = [h async for h in provider.search("zzzqqqxxx")]
            assert hits == []
    _run(body())


def test_running_the_calendar_row_opens_the_calendar():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            run = next(r for t, _h, r in _table(a) if t == "Calendar")
            run()
            await pilot.pause()
            assert isinstance(a.screen, CalendarScreen)
    _run(body())


def test_running_new_scheduled_job_opens_the_builder_on_daily(tmp_path):
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            run = next(r for t, _h, r in _table(a) if t == "New scheduled job")
            run()
            await pilot.pause()
            ed = a.screen
            assert isinstance(ed, JobScreen)
            assert ed.query_one("#job-preset", Select).value == "daily", (
                "palette creation has no day context — it should open daily"
            )

            # and the whole loop: fill, save, persisted
            ed.query_one("#job-prompt", m.Input).value = "morning check"
            await pilot.click("#job-save")
            await pilot.pause()
            assert [j.prompt for j in a.jobs] == ["morning check"]
            assert [j.prompt for j in sched_mod.load(tmp_path)] == ["morning check"]
    _run(body())


def test_running_the_tools_row_opens_the_list_and_does_NOT_toggle():
    """This row used to toggle. Ryan asked for it to show the list instead:
    "remove the func of /tools switching on and off and make it show this
    list please." (T076)

    📌 THE OLD PROPERTY WAS NOT DROPPED, IT MOVED. That the toggle verb still
    flips the flag, rebuilds the prompt and persists is asserted in
    tests/test_toggle_tools_persists.py, where the verb now lives. Ctrl+T
    still calls it — only the palette row and /tools changed.
    """
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            before = a.tools_enabled
            run = next(r for t, _h, r in _table(a) if t == "Tools")
            run()
            for _ in range(20):
                await pilot.pause()
                if a.screen.query("ToolListBody"):
                    break
            assert a.screen.query("ToolListBody"), "the row did not open the tool list"
            assert a.tools_enabled is before, "the row still toggled — it must only show"
    _run(body())


def test_the_real_palette_reaches_our_rows():
    """click the footer button, type, enter — the stock palette UI on OUR rows.

    🔴 THIS DROVE ctrl+p UNTIL T573, AND THE EXPECTATION IS STALE BY RULING,
    NOT BY ACCIDENT. T558 (9660da1) took ctrl+p for plan mode and the palette
    lost its only route; Ryan chose which one keeps the key — liteask
    a-5d6c1ca0, 2026-09-10 21:3x: "Keep plan on Ctrl+P, move the palette —
    palette via click only". So the door this arm drives moved on purpose.
    """
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            await pilot.click(PaletteButton)
            await pilot.pause()
            for ch in "calendar":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            await pilot.pause()
            assert isinstance(a.screen, CalendarScreen), (
                f"the palette flow ended on {type(a.screen).__name__}"
            )
    _run(body())


# --------------------------------------------------------------------------
# the header
# --------------------------------------------------------------------------

def test_the_tool_count_is_derived_not_hardcoded():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            a.tools_enabled = True
            a._update_header()
            expected = len(a._all_tools())
            assert f"tools:{expected}" in a.sub_title, (
                f"header says {a.sub_title!r}, _all_tools() says {expected}"
            )
            assert expected > 4, (
                "this machine offers more than the four the old literal named; "
                "if this ever equals 4 the derivation test is vacuous here"
            )
    _run(body())


def test_the_hardcoded_four_is_gone_from_the_source():
    """Gate the INSTRUCTION, never the STRING: the comment explaining this
    fix quotes "tools:4" in order to retract it, so a bare grep matches the
    retraction and cries wolf. The assignment is the defect; gate that."""
    assert 'mode = "tools:4"' not in APP_SRC, "the hardcoded count is back"


def test_tools_off_says_so():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            a.tools_enabled = False
            a._update_header()
            assert "no tools" in a.sub_title
    _run(body())


def test_the_working_directory_is_in_the_header():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            a._update_header()
            cwd = str(Path.cwd())
            home = str(Path.home())
            shown = "~" + cwd[len(home):] if cwd.startswith(home) else cwd
            assert shown in a.sub_title, (
                f"header {a.sub_title!r} does not carry the cwd {shown!r}"
            )
    _run(body())


def test_seat_registration_grows_the_count_by_the_harness_tool():
    """The one late-arriving tool. The count before and after registration
    must differ by exactly one, and the header must repaint to match."""
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)):
            a.tools_enabled = True
            a.seat.registered = False
            before = len(a._all_tools())
            a.seat.registered = True
            after = len(a._all_tools())
            assert after == before + 1

            a._update_header()
            assert f"tools:{after}" in a.sub_title
    _run(body())


# --------------------------------------------------------------------------
# the key itself
# --------------------------------------------------------------------------

def test_the_palette_is_reachable_at_all():
    """The palette has a door. This is the arm the last regression needed.

    ⚠️ IT ASKS THE SCREEN, NOT THE BINDING TABLE. A binding assertion would
    now be wrong by ruling: ctrl+p SHOULD be plan mode, and the shadow is
    intentional. What must stay true is that a user can still get to the
    palette, so this drives the real door and looks at what came up.

    🔴 `a.screen.query(CommandPalette)` RETURNS 0 HERE AND WOULD READ AS A
    FALSE NEGATIVE. Textual PUSHES the palette as a screen, so it is
    `a.screen` itself, not a descendant of it — measured, screen_stack reads
    ['Screen', 'CommandPalette']. The click only SCHEDULES the push, so the
    state is awaited rather than read on the next line.
    """
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            await pilot.click(PaletteButton)
            for _ in range(20):
                await pilot.pause()
                if isinstance(a.screen, CommandPalette):
                    break
            assert isinstance(a.screen, CommandPalette), (
                "clicking the footer button did not open the palette; the screen "
                f"stack is {[type(s).__name__ for s in a.screen_stack]}"
            )
    _run(body())


def test_the_palette_button_is_a_button_not_a_bar():
    """`width: auto` on .palette-button, pinned.

    Without it `dock: right` alone gives the button the FULL footer width — it
    still works, because it is composed last and wins the hit test, so every
    behavioural arm above stays green while a bold 190-column bar sits across
    the footer. Appearance was the thing no arm here could see, which is
    exactly how it shipped unnoticed in the label beside it (that one is
    Region(x=0, width=190) to this day and is NOT changed by this commit).
    """
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            # ⚠️ GEOMETRY IS AWAITED, NOT READ ON THE NEXT LINE. This arm
            # failed once inside the full-file run and passed alone: layout had
            # not settled on the first pause, so it measured the pre-layout
            # width. A single pause is a bet on how loaded the box is, which is
            # the same trap T565 was about -- wait for the value to exist.
            button = a.screen.query_one(PaletteButton)
            # Two separate questions, and conflating them is what made this arm
            # flaky: FIRST wait for layout to have happened at all (region is
            # 0x0 until it has), THEN judge the size. The bound is generous
            # because the slow case is real -- the first run after an edit
            # recompiles the module and startup takes measurably longer, which
            # is how this failed twice while passing 3x on the runs after.
            for _ in range(200):
                if button.region.width:
                    break
                await pilot.pause()
            assert button.region.width, (
                "the palette button never got a layout pass, so its size says "
                "nothing; this is a harness problem, not a width problem"
            )
            assert 0 < button.region.width < 40, (
                f"the palette button is {button.region.width} columns wide; "
                "it should size to its label, not span the footer"
            )
    _run(body())


def test_every_clickable_footer_widget_owns_its_own_cells():
    """A control that does not own its cells is a control nobody can click.

    🔴 THIS IS THE INVARIANT, NOT "nothing overlaps" (T578). Overlap is not
    satisfiable here and the card was wrong to ask for it: `.ctx-label` is
    Region(x=0, width=190) — the full footer row — and `dock: right` on a
    sibling does not stack, it places both against the same edge, so the label
    and the palette button cover the same cells BY DESIGN. Adding `width: auto`
    to the label does not fix that either; measured, the label becomes
    Region(x=103, width=87) and still covers the button at 176..190.

    What keeps the button clickable is that `ContextFooter` composes it LAST, so
    it wins the hit test on the cells they share — which was an undocumented
    ordering constraint until this arm. The next clickable chip composed BEFORE
    the label would render, report visible and display True, hold a non-zero
    region, and never receive a click; no behavioural arm can see that, because
    the widget is perfect and only the hit test disagrees.

    So the question asked here is the one a mouse asks: for every cell of every
    clickable child, who does `get_widget_at` say is there?
    """
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            # Layout first and judged separately — a region is 0x0 until it has
            # happened, and cell ownership before then means nothing.
            for _ in range(200):
                if a.screen.query_one(PaletteButton).region.width:
                    break
                await pilot.pause()
            footer = a.screen.query_one(ContextFooter)
            clickable = [c for c in footer.children
                         if hasattr(type(c), "on_click") and c.region.area]
            assert clickable, (
                "no clickable footer widget was found laid out; this arm is "
                "measuring nothing"
            )
            for widget in clickable:
                stolen = []
                for x in range(widget.region.x, widget.region.right):
                    hit = a.screen.get_widget_at(x, widget.region.y)
                    if hit and hit[0] is not widget:
                        stolen.append((x, type(hit[0]).__name__))
                assert not stolen, (
                    f"{type(widget).__name__} {widget.region} does not own "
                    f"{len(stolen)} of its cells — {stolen[:4]} — so a click "
                    "there goes to the other widget. Compose it after the "
                    "widget that covers it, or stop that widget covering it."
                )
    _run(body())
