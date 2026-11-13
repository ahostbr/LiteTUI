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
