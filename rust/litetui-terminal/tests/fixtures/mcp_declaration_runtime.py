"""Private shared declarations through the real registered host RPC child."""

import json
import os
import runpy
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
secret = "PRIVATE_MCP_SENTINEL"
attempts = []


def forbidden(*_args, **_kwargs):
    attempts.append("MCP lifecycle attempt")
    raise AssertionError(attempts[-1])


async def serve(app):
    from litetui import gui_rpc

    primary = {"mcpServers": {"primary": {"command": secret, "args": [secret]}}}
    secondary = {
        "shared-extra": secret,
        "mcpServers": {
            "offline": {
                "url": "https://example.invalid/" + secret,
                "headers": {"Authorization": secret},
                "env": {"TOKEN": secret},
            }
        },
    }
    assert app.mcp.root == root
    (root / "mcp.json").write_text(json.dumps(primary), encoding="utf-8")
    (root / ".mcp.json").write_text(json.dumps(secondary), encoding="utf-8")
    actual = gui_rpc.dispatch

    def dispatch(owner, command):
        if not command["type"].startswith("gui.mcp.native_config_"):
            return actual(owner, command)
        with ExitStack() as guards:
            for name in (
                "load",
                "connect",
                "reconnect",
                "disconnect",
                "reload_configs",
                "reconcile",
                "stop_all",
                "describe",
                "_build",
            ):
                guards.enter_context(patch.object(owner.mcp, name, forbidden))
            answer = actual(owner, command)
            assert secret not in json.dumps(answer)
            return answer

    try:
        with patch.object(gui_rpc, "dispatch", dispatch):
            await shared["serve"](app)
    finally:
        (root / "mcp-declaration-guards.json").write_text(
            json.dumps(attempts), encoding="utf-8"
        )


shared["history"].serve = serve
if __name__ == "__main__":
    shared["history"].main()
