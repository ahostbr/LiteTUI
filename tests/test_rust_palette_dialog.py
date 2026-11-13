"""native_palette_dialog: Textual's own CommandPalette (and its theme search), run headless."""
from __future__ import annotations

import pytest

from litetui import native_palette_dialog as dlg

SIZE = [140, 45]


def cmd(name, **extra):
    return {"type": dlg.PREFIX + name, "id": "x", "version": 1, **extra}


def act(action, query=None):
    values = {} if query is None else {"query": query}
    return cmd("act", size=SIZE, values=values, action=action)


def titles(snap):
    return ["".join(r["text"] for r in option[0]) for option in snap["list"]["options"]]


@pytest.mark.asyncio
async def test_theme_row_opens_pythons_theme_search_and_a_pick_applies_and_closes(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)):
        before = str(app.theme)
        snap = await dlg.dispatch(app, cmd("open", size=SIZE))
        assert snap["screen"] == "commands" and snap["size"] == SIZE
        assert snap["placeholder"].startswith("Search for commands")
        assert "Theme · App" in titles(snap)
        help_runs = snap["list"]["options"][titles(snap).index("Theme · App")][1]
        assert any("muted" in run["style"] for run in help_runs)        # $text-muted help
        snap = await dlg.dispatch(app, act(None, query="Theme"))
        assert titles(snap)[0].startswith("Theme")                       # Python's ranking
        snap = await dlg.dispatch(app, act({"key": "enter"}))
        assert snap["screen"] == "themes" and "routed" not in snap
        names = titles(snap)
        assert before in names and len(names) > 5
        target = next(n for n in names if n != before)
        snap = await dlg.dispatch(app, act({"option": names.index(target)}))
        assert snap["screen"] == "closed" and snap["theme"] == target == str(app.theme)
        assert tuple(app.size) == (80, 24)


@pytest.mark.asyncio
async def test_any_other_row_closes_the_palette_and_names_the_row_for_the_native_route(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)):
        await dlg.dispatch(app, cmd("open", size=SIZE))
        snap = await dlg.dispatch(app, act(None, query="Settings"))
        assert titles(snap)[0].startswith("Settings")
        depth = len(app.screen_stack)
        snap = await dlg.dispatch(app, act({"key": "enter"}))
        assert snap["screen"] == "closed" and snap["routed"].startswith("Settings")
        assert len(app.screen_stack) == depth - 1                         # nothing else opened
        snap = await dlg.dispatch(app, cmd("open", size=SIZE))
        snap = await dlg.dispatch(app, act({"key": "escape"}))
        assert snap["screen"] == "closed" and "routed" not in snap
