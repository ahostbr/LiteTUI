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
                "Toggle agent tools", "Help",
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


def test_running_toggle_tools_flips_and_the_header_follows():
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            before = a.tools_enabled
            run = next(r for t, _h, r in _table(a) if t == "Toggle agent tools")
            run()
            await pilot.pause()
            assert a.tools_enabled is (not before)
    _run(body())


def test_the_real_palette_reaches_our_rows():
    """ctrl+p, type, enter — the stock palette UI running OUR provider."""
    async def body():
        a = make_app()
        async with a.run_test(size=(190, 48)) as pilot:
            await pilot.press("ctrl+p")
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
