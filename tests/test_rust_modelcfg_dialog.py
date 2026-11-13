"""native_modelcfg_dialog: Python's own ModelConfigScreen, run headless for the native /modelcfg."""
from __future__ import annotations

import pytest

from litetui import native_modelcfg_dialog as dlg

SIZE = [140, 45]


def cmd(name, **extra):
    return {"type": dlg.PREFIX + name, "id": "x", "version": 1, **extra}


def act(action, values=None):
    return cmd("act", size=SIZE, values=values or {}, action=action)


def flat(items):
    for entry in items:
        yield entry
        yield from flat(entry.get("items", ()))


@pytest.mark.asyncio
async def test_open_tabs_and_escape_run_pythons_screen_at_the_frontends_size(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)):
        app._rpc = True
        app.model_id = "perf-fixture"
        snap = await dlg.dispatch(app, cmd("open", size=SIZE))
        assert snap["screen"] == "form" and snap["size"] == SIZE
        assert snap["box"][2] == 92            # #set-box width 92, laid out at 140 columns
        entries = list(flat(snap["items"]))
        tabs = next(e for e in entries if e["kind"] == "tabs")
        assert [t["label"] for t in tabs["tabs"]] == ["Info", "Load", "Inference"]
        assert tabs["active"].endswith("mc-info")
        snap = await dlg.dispatch(app, act({"tab": "mc-load"}))
        entries = list(flat(snap["items"]))
        scroll = next(e for e in entries if "clip" in e)
        assert scroll["max_scroll_y"] > 0      # the Load tab is taller than its pane
        assert any(e.get("id") == "ld-ctx" and e["kind"] == "input" for e in entries)
        snap = await dlg.dispatch(app, act({"scroll": 5}))
        assert next(e for e in flat(snap["items"]) if "clip" in e)["scroll_y"] == 5
        snap = await dlg.dispatch(app, act({"key": "escape"}))
        assert snap["screen"] == "closed"
        assert tuple(app.size) == (80, 24)     # restored once the screen is gone
        app._rpc = False


@pytest.mark.asyncio
async def test_no_model_and_remote_backends_open_nothing_as_python_does(make_app):
    app = make_app()
    async with app.run_test(size=(80, 24)):
        app._rpc = True
        app.model_id = ""
        assert (await dlg.dispatch(app, cmd("open", size=SIZE)))["screen"] == "closed"
        assert tuple(app.size) == (80, 24)
        app.backend.remote = True
        try:
            assert (await dlg.dispatch(app, cmd("open", size=SIZE)))["screen"] == "remote"
        finally:
            del app.backend.remote
        app._rpc = False
