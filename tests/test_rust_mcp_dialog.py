"""native_mcp_dialog: Python's own MCPListBody, run headless for the native /mcp."""
from __future__ import annotations

import pytest

from litetui import native_mcp_dialog as dlg


def cmd(name, **extra):
    return {"type": dlg.PREFIX + name, "id": "x", "version": 1, **extra}


@pytest.mark.asyncio
async def test_open_is_the_body_in_order_with_its_rows_nested_and_close_pops_it(make_app):
    app = make_app()
    async with app.run_test(size=(140, 45)) as pilot:
        base = len(app.screen_stack)
        snap = await dlg.dispatch(app, cmd("open"))
        kinds = [entry["kind"] for entry in snap["items"]]
        assert kinds[:4] == ["text", "text", "scroll", "line"]
        assert snap["items"][0]["text"] == "MCP servers"
        rows = snap["items"][2]["items"]
        assert rows and rows[0]["classes"] == ["mcp-detail"]
        add = snap["items"][3]
        assert add["id"] == "mcp-add"
        assert [e.get("id") for e in add["items"]] == ["mcp-add-name", "mcp-add-target", "mcp-add-go"]
        # An empty add answers in the body's own status line.
        snap = await dlg.dispatch(app, cmd("act", values={}, action={"press": "mcp-add-go"}))
        status = next(e for e in snap["items"] if e.get("id") == "mcp-status")
        assert status["text"]
        await dlg.dispatch(app, cmd("close"))
        await pilot.pause()
        assert len(app.screen_stack) == base
        with pytest.raises(ValueError, match="not open"):
            await dlg.dispatch(app, cmd("read"))


@pytest.mark.asyncio
async def test_an_add_driven_from_the_rpc_management_worker_is_not_deferred_by_that_worker(make_app):
    """rpc._dispatch runs every gui.* command in a gui-management worker, and the press is
    handled while it runs. MCPListBody's gate leaves out only its own host worker, so the act
    must name its worker as the host, or the add answers "Deferred (busy)" against itself."""
    app = make_app()
    async with app.run_test(size=(140, 45)) as pilot:
        await dlg.dispatch(app, cmd("open"))
        # The test app's inbox and cron pollers would defer Python's own dialog too; stop them
        # so the act's own worker is the only one left to count.
        for other in [w for w in app.workers if w.group in ("inbox", "cron")]:
            other.cancel()
        await pilot.pause()

        async def add():
            return await dlg.dispatch(app, cmd(
                "act", values={"mcp-add-name": "fixmcp", "mcp-add-target": "http://127.0.0.1:9/mcp"},
                action={"press": "mcp-add-go"}))

        worker = app.run_worker(add(), group="gui-management", exit_on_error=False)
        await worker.wait()
        snap = worker.result
        for _ in range(100):
            if not snap["testing"]:
                break
            await pilot.pause(0.05)
            snap = await dlg.dispatch(app, cmd("read"))
        status = next(e for e in snap["items"] if e.get("id") == "mcp-status")["text"]
        assert "Deferred" not in status and "fixmcp" in status, status
        body = dlg._body(app)
        assert getattr(body, "_dialog_host_worker", None) is None
        await dlg.dispatch(app, cmd("close"))
