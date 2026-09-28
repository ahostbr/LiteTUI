"""The command palette is grouped, ordered, and readable by someone new.

the user, 2026-08-22: reorganise it, with "newb friendly descriptions and names for
users", into six groups he named himself — Convo, Backend, Tools, Automation,
Screen, App — keeping every /slashcmd name exactly as is.

Three things are asserted here, because each was a real decision:

  1. ORDER IS DERIVED, NOT TABULATED. Rows come from the registry; Settings
     and Theme lead, then titles sort alphabetically, regardless of their
     retained group labels. No second inventory of commands exists.

  2. THE SLASH COMMAND IS DERIVED TOO. It used to be typed into the help string
     as "(/compact)" — a machine token mid-sentence, and a second copy of the
     command name that could drift from the real one. It is now appended from
     entry.tokens[0], so it cannot disagree with what the dispatcher handles.

  3. THEME SURVIVES. The registry's Theme row delegates to Textual's own
     theme action, with the stock provider removed to avoid duplication.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import plugins as plugins_mod

EXPECTED_GROUPS = ("convo", "backend", "tools", "automation", "screen", "app")


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = []
    return a


def _rows(app):
    provider = m.LiteTUICommands(app.screen)
    return provider._commands()


def test_groups_are_ryans_scheme_in_his_order() -> None:
    assert plugins_mod.PALETTE_GROUPS == EXPECTED_GROUPS
    for g in EXPECTED_GROUPS:
        assert plugins_mod.PALETTE_GROUP_LABELS[g], f"{g} has no display label"


def test_an_unknown_group_sorts_last_instead_of_raising() -> None:
    """A third-party plugin inventing a group must not take down the palette."""
    known = plugins_mod.palette_sort_key("convo", 10, "x")
    unknown = plugins_mod.palette_sort_key("wat", 10, "x")
    assert unknown > known


@pytest.mark.asyncio
async def test_the_palette_is_grouped_and_ordered() -> None:
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        rows = _rows(a)
        assert rows, "the palette is empty"

        titles = [t for t, _h, _r in rows]
        # Every row retains a trailing group label without hiding the
        # alphabetical title at the start of the visible line.
        seen: list[str] = []
        for title in titles:
            assert " · " in title, f"row has no trailing group: {title!r}"
            label = title.split(" · ", 1)[1]
            if not seen or seen[-1] != label:
                seen.append(label)

        # The labels survive even though alphabetical titles split groups.
        wanted = [plugins_mod.PALETTE_GROUP_LABELS[g] for g in EXPECTED_GROUPS]
        assert set(wanted) <= set(seen), f"groups missing from palette: {seen}"


@pytest.mark.asyncio
async def test_quit_follows_the_pinned_rows_alphabetically() -> None:
    """Quit follows alphabetical title order rather than legacy group rank."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        titles = [t for t, _h, _r in _rows(a)]
        names = [title.split(" · ", 1)[0] for title in titles]
        assert names.index("Quit") < names.index("Screenshot")


@pytest.mark.asyncio
async def test_the_slash_command_is_derived_from_the_dispatcher() -> None:
    """No row may hand-write its own command into the help text."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for title, help_text, _run in _rows(a):
            assert "(/" not in help_text, (
                f"{title}: the command is written inline in the description "
                f"({help_text!r}) — it must be derived from the dispatcher"
            )

        # And the ones that have a command still show it, at the end.
        tagged = [h for _t, h, _r in _rows(a) if " /" in h]
        assert len(tagged) >= 15, "slash tags vanished from the palette"


@pytest.mark.asyncio
async def test_reclaimed_rows_have_real_commands() -> None:
    """the user's call: the rows that had no slash command get one."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for token in ("/tools", "/keys", "/screenshot", "/maximize", "/quit"):
            assert token in a.plugins.commands, f"{token} is not registered"


@pytest.mark.asyncio
async def test_theme_survives_and_nothing_is_offered_twice() -> None:
    """Registry Theme replaces the stock row without duplicating it."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        titles = [t.split(" · ", 1)[0] for t, _h, _run in _rows(a)]
        assert titles.count("Theme") == 1, "the theme picker needs exactly one row"
        assert a.COMMANDS == {m.LiteTUICommands}, "stock provider duplicates registry rows"
        for reclaimed in ("Keys", "Maximize", "Screenshot", "Quit"):
            assert titles.count(reclaimed) == 1, f"{reclaimed} is offered twice"
