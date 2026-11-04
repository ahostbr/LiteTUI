"""Two private cache snapshots around real Runtime status reads, with all MCP I/O refused."""
from __future__ import annotations

import json
import os
import runpy
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
secret = "SYNTHETIC_MCP_SECRET_MUST_STAY_PRIVATE"
samples = []
attempts = []


def forbidden(*_args, **_kwargs):
    attempts.append("MCP lifecycle or configuration I/O")
    raise AssertionError(attempts[-1])


async def serve(app):
    from litetui import gui_rpc
    actual_dispatch = gui_rpc.dispatch

    def dispatch(owner, command):
        if command["type"] != "gui.inspection.native_status":
            return actual_dispatch(owner, command)
        read = len(samples)
        assert read < 2
        mgr = owner.mcp
        # No real endpoint, process, headers, arguments or tools are used. These
        # are the already-existing manager's in-memory source representations.
        configs = {"cached-alpha": {"url": "https://example.invalid/" + secret, "headers": {"Authorization": secret}},
                   "cached-failed": {"command": secret, "args": [secret], "env": {"FIXTURE": secret}}}
        configs.update({f"fixture-{number:02}": {"command": secret} for number in range(37)})
        server = SimpleNamespace(error=None, tools=[{"name": secret}] * (2 + read))
        with ExitStack() as guards:
            for attr, value in (("configs", configs), ("servers", {"cached-alpha": server}),
                                ("failures", {"cached-failed": secret})):
                guards.enter_context(patch.object(mgr, attr, value))
            if read:
                guards.enter_context(patch.object(owner.store, "persist_error", secret))
                guards.enter_context(patch.object(owner, "_settings_persist_error", secret, create=True))
            for method in ("load", "connect", "reconnect", "disconnect", "reload_configs", "reconcile",
                           "stop_all", "describe"):
                guards.enter_context(patch.object(mgr, method, forbidden))
            result = actual_dispatch(owner, command)
            assert secret not in json.dumps(result, ensure_ascii=False)
            samples.append({"cached_alpha_tools": len(server.tools), "declared": len(configs), "app_mcp_enabled": app.settings.mcp_enabled})
            return result

    try:
        with patch.object(gui_rpc, "dispatch", dispatch):
            await shared["serve"](app)
    finally:
        (root / "inspection-boundaries.json").write_text(json.dumps({"samples": samples, "attempts": attempts}), encoding="utf-8")


shared["history"].serve = serve

if __name__ == "__main__":
    shared["history"].main()
