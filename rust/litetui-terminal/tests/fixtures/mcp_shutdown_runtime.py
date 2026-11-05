"""One disposable source-owned stdio server, never an installed MCP service."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from litetui import mcp_client, ttyguard

real_popen = subprocess.Popen
real_disconnect = mcp_client.MCPManager.disconnect
shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
report = {"exit_joined": False, "lease_joined": False, "spawns": 0}


async def serve(app):
    from litetui import gui_rpc, native_mcp_lifecycle, native_mcp_shutdown

    source = Path(os.environ["RUST_RUNTIME_CONTROLS_SOURCE"])
    assert (
        Path(native_mcp_shutdown.__file__).resolve()
        == source / "src/litetui/native_mcp_shutdown.py"
    )
    manager = app.mcp
    assert manager is app._owned_mcp_manager and not app.settings.mcp_enabled
    assert manager.root == root and not manager.servers
    script = root / "private-mcp.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " message=json.loads(line)\n"
        " if 'id' not in message: continue\n"
        " method=message['method']\n"
        " assert method in ('initialize','tools/list')\n"
        " result={'tools':[{'name':'private','inputSchema':{'type':'object'}}]} if method=='tools/list' else {'capabilities':{}}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)\n",
        encoding="utf-8",
    )

    def spawn(argv, **kwargs):
        assert argv == [sys.executable, "-u", str(script)]
        assert Path(kwargs["cwd"]) == root
        report["spawns"] += 1
        assert report["spawns"] == 1
        kwargs.update(text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return real_popen(argv, **kwargs)

    cfg = {"command": sys.executable, "args": ["-u", str(script)]}
    server = manager._build("private", cfg)
    manager.configs["private"] = cfg
    manager.servers["private"] = server
    proc = reader = None
    stop = server.stop
    actual_exit = app.exit
    actual_release = app._release_owned_storage

    def failed_once():
        report["stop_calls"] = report.get("stop_calls", 0) + 1
        if report["stop_calls"] == 1:
            raise mcp_client.MCPError("PRIVATE SENTINEL must not become a success")
        stop()

    def joined():
        assert proc is not None and proc.poll() is not None
        assert reader is not None and not reader.is_alive()
        assert not manager.servers and not manager._stop_failed and manager._closing

    def exit_app(*args, **kwargs):
        joined()
        report["exit_joined"] = True
        return actual_exit(*args, **kwargs)

    async def release():
        joined()
        report["lease_joined"] = True
        return await actual_release()

    try:
        with patch.object(ttyguard, "popen", spawn):
            server.start()
        proc, reader = server.proc, server._reader
        assert proc.poll() is None and reader.is_alive()
        report["pid"] = proc.pid
        server.stop = failed_once
        assert real_disconnect(manager, "private") is True
        assert manager.servers["private"] is server and proc.poll() is None
        report["retained"] = manager.inspect_cached()
        assert report["retained"]["servers"][0]["state"] == "failed"
        with (
            patch.object(gui_rpc, "OPERATIONS", tuple(op for op in gui_rpc.OPERATIONS if "gui." + op not in native_mcp_lifecycle.OPERATIONS)),
            patch.object(app, "exit", exit_app),
            patch.object(app, "_release_owned_storage", release),
        ):
            await shared["serve"](app)
    finally:
        report["proc_exited"] = proc is not None and proc.poll() is not None
        report["reader_joined"] = reader is not None and not reader.is_alive()
        report["mcp_enabled"] = app.settings.mcp_enabled
        server.stop = stop
        manager.stop_all()
        (root / "mcp-shutdown.json").write_text(json.dumps(report), encoding="utf-8")


shared["history"].serve = serve

if __name__ == "__main__":
    shared["history"].main()
