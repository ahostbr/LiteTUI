"""Actual owned Work binding and private declaration files. No MCP I/O."""

import json
import os

import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import gui_rpc, native_mcp_config

app, switch_app, armed = owned_app, owned_switch_app, owned_context
pytestmark = pytest.mark.asyncio


def call(app, op, **kw):
    return gui_rpc.dispatch(
        app,
        {
            "type": native_mcp_config.PREFIX + op,
            "id": "owned-" + op,
            "version": 1,
            **work.identity(app),
            "work_instance": work.bridge(app).instance,
            **kw,
        },
    )


async def test_redacted_shared_declaration_edit_rereads_both_files(armed, monkeypatch):
    app = armed
    manager = app.mcp
    root = manager.root
    private = "private-sentinel-do-not-emit"
    (root / "mcp.json").write_text(
        json.dumps({"servers": {"pinned": {"url": "https://" + private}}})
    )
    original = {
        "keep": private,
        "mcpServers": {
            "pinned": {"command": private},
            "edit": {"command": private, "args": [private], "env": {"TOKEN": private}},
        },
    }
    dot = root / ".mcp.json"
    dot.write_text(json.dumps(original))
    for verb in (
        "load",
        "connect",
        "reconnect",
        "disconnect",
        "reconcile",
        "reload_configs",
    ):
        monkeypatch.setattr(
            manager, verb, lambda *a: pytest.fail("unexpected MCP runtime operation")
        )
    result = call(app, "list")
    assert private not in json.dumps(result)
    assert [
        (r["file"], r["name"], r["shadowed"], r["editable"]) for r in result["rows"]
    ] == [
        ("mcp.json", "pinned", False, False),
        (".mcp.json", "pinned", True, False),
        (".mcp.json", "edit", False, True),
    ]
    with pytest.raises(ValueError):
        call(app, "review", name="pinned", revision=result["revision"], disabled=True)
    review = call(
        app, "review", name="edit", revision=result["revision"], disabled=True
    )
    assert json.loads(dot.read_bytes()) == original and private not in json.dumps(
        review
    )
    argv = {"token": review["token"]}
    saved = call(app, "apply", **argv)
    with pytest.raises(ValueError):
        call(app, "apply", **argv)
    proof = call(app, "readback", apply_id=saved["apply_id"], **argv)
    original["mcpServers"]["edit"]["disabled"] = True
    assert json.loads(dot.read_bytes()) == original
    assert (
        proof["config"]["rows"][-1]["disabled"]
        and proof["after"] == proof["config"]["revision"]
    )
    assert private not in json.dumps(proof)
    enable = call(app, "review", name="edit", revision=proof["after"], disabled=False)
    applied = call(app, "apply", token=enable["token"])
    restored = call(
        app, "readback", token=enable["token"], apply_id=applied["apply_id"]
    )
    assert restored["config"]["rows"][-1]["disabled"] is False


async def test_changed_readback_and_running_servers_refuse(armed):
    app = armed
    manager = app.mcp
    path = manager.root / ".mcp.json"
    path.write_text('{"mcpServers":{"keep":{"command":"offline"}}}')
    current = call(app, "list")
    manager.servers["keep"] = object()
    try:
        with pytest.raises(ValueError):
            call(
                app, "review", revision=current["revision"], name="keep", disabled=True
            )
    finally:
        manager.servers.pop("keep")
    review = call(
        app, "review", revision=current["revision"], name="keep", disabled=True
    )
    written = call(app, "apply", token=review["token"])
    path.write_text('{"mcpServers":{"keep":{"command":"changed"}}}')
    with pytest.raises(ValueError, match="changed after apply"):
        call(app, "readback", apply_id=written["apply_id"], token=review["token"])
    assert json.loads(path.read_bytes())["mcpServers"]["keep"] == {"command": "changed"}


async def test_ambiguous_legacy_target_and_large_inventory(armed):
    app = armed
    path = app.mcp.root / ".mcp.json"
    path.write_text('{"servers":{"one":{"command":"private"}}}')
    result = call(app, "list")
    assert result["rows"][0]["editable"] is False
    with pytest.raises(ValueError):
        call(app, "review", revision=result["revision"], name="one", disabled=True)
    path.write_text(
        json.dumps({"mcpServers": {str(i): {"command": "private"} for i in range(65)}})
    )
    with pytest.raises(ValueError, match="64-row"):
        call(app, "list")


async def test_foreign_changes_and_duplicate_keys_fail_closed(armed):
    app = armed
    root = app.mcp.root
    path = root / ".mcp.json"
    path.write_text('{"mcpServers":{"keep":{"command":"fake"}}}')
    revision = call(app, "list")["revision"]
    review = call(app, "review", name="keep", revision=revision, disabled=True)
    previous = path.read_bytes()
    (root / "mcp.json").write_text('{"servers":{"other":{"url":"secret"}}}')
    with pytest.raises(ValueError, match="changed"):
        call(app, "apply", token=review["token"])
    assert path.read_bytes() == previous
    path.write_text('{"secret":4,"secret":3}')
    with pytest.raises(ValueError, match="unambiguous"):
        call(app, "list")


async def test_linked_writer_lock_refuses_without_touching_either_file(armed):
    app = armed
    root = app.mcp.root
    target = root / ".mcp.json"
    target.write_text('{"mcpServers":{"keep":{"command":"private"}}}')
    sentinel = root / "unrelated.txt"
    sentinel.write_bytes(b"")
    os.link(sentinel, root / ".mcp.json.lock")
    snapshot = call(app, "list")
    review = call(app,"review",revision=snapshot["revision"], name="keep",disabled=True)
    before = target.read_bytes()
    with pytest.raises(ValueError,match="lock"):
        call(app,"apply",token=review["token"])
    assert sentinel.read_bytes() == b"" and target.read_bytes() == before
