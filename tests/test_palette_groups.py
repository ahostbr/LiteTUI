"""The command palette is grouped, ordered, and readable by someone new.

Ryan, 2026-08-22: reorganise it, with "newb friendly descriptions and names for
users", into six groups he named himself — Convo, Backend, Tools, Automation,
Screen, App — keeping every /slashcmd name exactly as is.

Three things are asserted here, because each was a real decision:

  1. ORDER IS DERIVED, NOT TABULATED. Every row declares its own group and rank
     at the point it is registered. The provider sorts; it owns no list. A
     hand-authored second table is the drift class the derived palette already
     replaced, and it would silently drop any command nobody remembered to add.

  2. THE SLASH COMMAND IS DERIVED TOO. It used to be typed into the help string
     as "(/compact)" — a machine token mid-sentence, and a second copy of the
     command name that could drift from the real one. It is now appended from
     entry.tokens[0], so it cannot disagree with what the dispatcher handles.

  3. THEME SURVIVES. Textual's stock provider is filtered, not dropped, because
     it is the only source of the theme picker and /settings promises in writing
     that ctrl+p still has one.
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
    a._jobs = []
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
        # Every row is prefixed with its group label — the grouping has to be
        # VISIBLE, since Textual's palette has no section headers.
        seen: list[str] = []
        for title in titles:
            assert "›" in title, f"row is not group-prefixed: {title!r}"
            label = title.split("›")[0].strip()
            if not seen or seen[-1] != label:
                seen.append(label)

        # Groups appear as contiguous blocks, in Ryan's order. A label showing
        # up twice means the sort is not actually grouping.
        assert len(seen) == len(set(seen)), f"a group is split across the list: {seen}"
        wanted = [plugins_mod.PALETTE_GROUP_LABELS[g] for g in EXPECTED_GROUPS]
        assert seen == [w for w in wanted if w in seen], f"groups out of order: {seen}"


@pytest.mark.asyncio
async def test_quit_is_the_last_row() -> None:
    """Nothing destructive sits near anything frequent."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        titles = [t for t, _h, _r in _rows(a)]
        assert titles[-1].endswith("Quit"), f"last row is {titles[-1]!r}, expected Quit"


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
    """Ryan's call: the rows that had no slash command get one."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        for token in ("/tools", "/keys", "/screenshot", "/maximize", "/quit"):
            assert token in a.plugins.commands, f"{token} is not registered"


@pytest.mark.asyncio
async def test_theme_survives_and_nothing_is_offered_twice() -> None:
    """The stock provider is FILTERED, not dropped — theme is the whole reason."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        stock = [c.title for c in a.get_system_commands(a.screen)]
        assert "Theme" in stock, (
            "the theme picker is gone from ctrl+p, and /settings promises it is there"
        )
        for reclaimed in ("Keys", "Maximize", "Screenshot", "Quit"):
            assert reclaimed not in stock, (
                f"{reclaimed} is offered by BOTH providers — it would appear twice, "
                "in two different vocabularies"
            )
