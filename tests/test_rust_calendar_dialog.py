"""native_calendar_dialog: Python's own CalendarScreen chain, run headless for the native /cal."""
from __future__ import annotations

from datetime import date

import pytest

from litetui import native_calendar_dialog as dlg

SIZE = [140, 45]


def cmd(name, **extra):
    return {"type": dlg.PREFIX + name, "id": "x", "version": 1, **extra}


def act(action, values=None):
    return cmd("act", size=SIZE, values=values or {}, action=action)


def plain(lines):
    return ["".join(run["text"] for run in line) for line in lines]


@pytest.mark.asyncio
async def test_the_chain_month_day_job_and_back_runs_pythons_screens_over_rpc(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)) as pilot:
        app._rpc = True          # present_dialog refuses over --rpc unless the frontend drives
        base = len(app.screen_stack)
        today = date.today()
        snap = await dlg.dispatch(app, cmd("open", size=SIZE))
        assert snap["screen"] == "calendar" and (snap["year"], snap["month"]) == (today.year, today.month)
        grid = plain(snap["grid"]["lines"])
        assert grid[0].startswith(today.strftime("%B").upper())
        assert any(f"{today.day}•" in row for row in grid)
        assert plain(snap["side"]["lines"])[0] == "JOBS"
        assert any(run["style"] for line in snap["grid"]["lines"] for run in line)   # the theme's colours
        # Laid out at the frontend's size: the 96% x 92% box, the grid beside the 34-wide pane.
        assert snap["size"] == SIZE and snap["box"][:3] == [3, 2, 134]
        assert snap["grid"]["region"][:3] == [6, 4, 94] and snap["side"]["region"][0] == 100
        # The click lands in the grid's own cells, where the painter's hit-map puts today.
        row = next(i for i, text in enumerate(grid) if f"{today.day}•" in text)
        col = grid[row].index(f"{today.day}•")
        snap = await dlg.dispatch(app, act({"grid_click": [col, row]}))
        assert snap["screen"] == "day"
        options = next(e for e in snap["items"] if e["kind"] == "options")
        assert plain(options["options"][-1]["lines"])[0].endswith("new job on this day…")
        snap = await dlg.dispatch(app, act({"key": "n"}))
        assert snap["screen"] == "job"
        ids = {e.get("id") for e in snap["items"]}
        assert {"job-prompt", "job-preset", "job-status"} <= ids
        assert snap["focused"] == "job-prompt" and snap["box"][2] == 82
        assert all(len(e["region"]) == 4 for e in snap["items"])
        tickers = [e for line in snap["items"] if line["kind"] == "line" for e in line["items"]
                   if e["kind"] == "ticker"]
        assert [t["id"] for t in tickers][:2] == ["job-hour", "job-minute"]
        snap = await dlg.dispatch(app, act({"tick": "job-hour", "delta": 1}))
        hour = next(e for line in snap["items"] if line["kind"] == "line" for e in line["items"]
                    if e.get("id") == "job-hour")
        assert hour["value"] == 10
        assert (await dlg.dispatch(app, act({"key": "escape"})))["screen"] == "day"
        assert (await dlg.dispatch(app, act({"key": "escape"})))["screen"] == "calendar"
        snap = await dlg.dispatch(app, act({"key": "right"}))
        assert snap["month"] == (today.month % 12) + 1
        assert (await dlg.dispatch(app, act({"key": "q"})))["screen"] == "closed"
        await pilot.pause()
        assert len(app.screen_stack) == base
        assert tuple(app.size) == (140, 45)     # run_test's own size, restored after the fit
        app._rpc = False


@pytest.mark.asyncio
async def test_without_the_frontend_driving_a_headless_open_is_still_refused(make_app):
    from litetui.plugins.scheduler_ui import CalendarBody

    app = make_app()
    async with app.run_test(size=(140, 45)) as pilot:
        app._rpc = True
        await dlg.dispatch(app, cmd("open", size=SIZE))
        body = app.screen.query_one(CalendarBody)
        depth = len(app.screen_stack)
        body.open_day(1)                 # not through an act: the --rpc guard holds
        await pilot.pause()
        assert len(app.screen_stack) == depth
        await dlg.dispatch(app, cmd("close"))
        app._rpc = False
