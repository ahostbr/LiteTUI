"""native_hooks_editor: Python's own HooksEditor, run headless for the native /hooks."""
from __future__ import annotations

import json

import pytest

from litetui import native_hooks_editor as ed


def items(snap, kind=None):
    out = []

    def walk(entries):
        for entry in entries:
            if entry["kind"] == "line":
                walk(entry["items"])
            elif kind is None or entry["kind"] == kind:
                out.append(entry)

    walk(snap["items"])
    return out


def by_id(snap, wid):
    return next(entry for entry in items(snap) if entry.get("id") == wid)


def cmd(name, **extra):
    return {"type": ed.PREFIX + name, "id": "x", "version": 1, **extra}


@pytest.mark.asyncio
async def test_open_is_the_editor_in_screen_order_and_close_pops_it(make_app):
    from litetui.hooks_screen import HooksScreen

    app = make_app()
    async with app.run_test(size=(140, 45)) as pilot:
        base = len(app.screen_stack)
        snap = await ed.dispatch(app, cmd("open"))
        await pilot.pause()
        assert isinstance(app.screen, HooksScreen)
        kinds = [entry["kind"] for entry in snap["items"]]
        assert kinds[:6] == ["text", "label", "select", "select", "line", "line"]
        assert by_id(snap, "hook-scope")["options"] == ["Project", "Global"]
        assert by_id(snap, "hook-scope")["value"] == "0"
        assert by_id(snap, "hook-select")["prompt"] == "Choose a hook"
        assert by_id(snap, "hook-field-id")["value"] == "check"
        # Hidden repair controls are not drawn.
        assert all(entry.get("id") not in ("hook-repair-json", "hook-repair") for entry in items(snap))
        assert snap["items"][0]["text"].startswith("Global: ")
        await ed.dispatch(app, cmd("close"))
        await pilot.pause()
        assert len(app.screen_stack) == base


@pytest.mark.asyncio
async def test_new_edit_and_save_run_the_editors_own_handlers(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)) as pilot:
        await ed.dispatch(app, cmd("open"))
        snap = await ed.dispatch(app, cmd("act", values={}, action={"press": "hook-new"}))
        assert by_id(snap, "hook-select")["options"] == ["new-hook · observe · enabled"]
        assert by_id(snap, "hook-error")["text"] == "Unsaved changes"
        snap = await ed.dispatch(app, cmd("act", values={"hook-field-id": "probe", "hook-enabled": False},
                                          action={"press": "hook-save"}))
        await pilot.pause()
        assert by_id(snap, "hook-error")["text"] == "Saved; applies to the next event"
        saved = json.loads(app.hook_config.path("project").read_text(encoding="utf-8"))
        hooks = saved["hooks"] if isinstance(saved, dict) else saved
        assert [h["id"] for h in hooks] == ["probe"] and hooks[0]["enabled"] is False
        # A pick runs the scope handler: Global has no hooks.
        snap = await ed.dispatch(app, cmd("act", values={}, action={"select": "hook-scope", "index": 1}))
        assert by_id(snap, "hook-scope")["value"] == "1"
        assert by_id(snap, "hook-select")["options"] == []
        await ed.dispatch(app, cmd("close"))


@pytest.mark.asyncio
async def test_bad_commands_change_nothing(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)):
        with pytest.raises(ValueError, match="not open"):
            await ed.dispatch(app, cmd("read"))
        await ed.dispatch(app, cmd("open"))
        for bad in ({"press": "hook-repair"}, {"select": "hook-scope", "index": 5}, {"press": "x", "y": 1}):
            with pytest.raises(Exception):
                await ed.dispatch(app, cmd("act", values={}, action=bad))
        with pytest.raises(ValueError, match="malformed"):
            await ed.dispatch(app, cmd("act", values={"hook-enabled": "yes"}, action=None))
        with pytest.raises(ValueError, match="fields"):
            await ed.dispatch(app, {"type": ed.PREFIX + "open", "version": 1, "extra": 1})
        await ed.dispatch(app, cmd("close"))
