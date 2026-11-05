"""Reviewed MCP Runtime through the registered reader and disposable stdio children."""

from __future__ import annotations

import copy
import json
import os
import runpy
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from litetui import mcp_client, ttyguard

real_popen = subprocess.Popen
real_connect = mcp_client.MCPManager.connect
real_reload = mcp_client.MCPManager.reload_configs
shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
report = {
    "spawns": 0,
    "operations": [],
    "exit_joined": False,
    "lease_joined": False,
    "approvals": [],
    "tool": None,
}


async def serve(app):
    from litetui import (
        gui_rpc,
        native_mcp_lifecycle,
        native_runtime_controls,
        rpc,
        tool_approval,
    )
    from litetui.plugins import PluginContext, mcp_plugin

    assert (
        Path(native_mcp_lifecycle.__file__).resolve()
        == Path(os.environ["RUST_RUNTIME_CONTROLS_SOURCE"])
        / "src/litetui/native_mcp_lifecycle.py"
    )
    manager = app.mcp
    assert (
        manager is app._owned_mcp_manager
        and manager.root == root
        and not manager.servers
        and app.settings.mcp_enabled is False
    )
    mcp_plugin._register(PluginContext(app, app.plugins, "mcp"))
    script = root / "private-lifecycle-mcp.py"
    calls = root / "private-lifecycle-calls.jsonl"
    script.write_text(
        "import json,sys,pathlib\n"
        f"calls=pathlib.Path({str(calls)!r})\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if 'id' not in m: continue\n"
        " method=m['method']\n"
        " assert method in ('initialize','tools/list','tools/call')\n"
        " result={'tools':[{'name':'private','inputSchema':{'type':'object','properties':{'value':{'type':'string'}}}}]} if method=='tools/list' else {'capabilities':{}}\n"
        " if method=='tools/call':\n"
        "  with calls.open('a',encoding='utf8') as f: f.write(json.dumps(m['params'])+'\\n')\n"
        "  result={'content':[{'type':'text','text':'CONTROLLED:'+m['params']['arguments']['value']}]}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)\n",
        encoding="utf-8",
    )
    config = {
        "mcpServers": {
            "private": {
                "command": sys.executable,
                "args": ["-u", str(script)],
                "env": {"PRIVATE_SENTINEL": "SECRET-MCP-LIFECYCLE"},
            }
        }
    }
    (root / ".mcp.json").write_text(json.dumps(config), encoding="utf-8")
    processes, servers, readers = [], [], []
    build, actual_gui = manager._build, gui_rpc.async_dispatch
    actual_emit = rpc.rpc_emit
    actual_exit, actual_release = app.exit, app._release_owned_storage
    actual_tool = app._execute_tool

    def spawn(argv, **kwargs):
        assert (
            argv == [sys.executable, "-u", str(script)] and Path(kwargs["cwd"]) == root
        )
        kwargs.update(text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = real_popen(argv, **kwargs)
        processes.append(process)
        report["spawns"] += 1
        assert report["spawns"] <= 3
        return process

    def created(name, cfg):
        assert name == "private" and cfg == config["mcpServers"][name]
        server = build(name, cfg)
        servers.append(server)
        start, stop = server.start, server.stop
        ordinal = len(servers)
        attempts = 0

        def started():
            start()
            readers.append(server._reader)

        def stopped():
            nonlocal attempts
            attempts += 1
            report.setdefault("stops", []).append(
                {"server": ordinal, "attempt": attempts}
            )
            if ordinal == 2 and attempts == 1:
                raise mcp_client.MCPError("SECRET stop failure remains private")
            return stop()

        server.start, server.stop = started, stopped
        return server

    async def management(owner, command):
        result = await actual_gui(owner, command)
        if command["type"].startswith(native_mcp_lifecycle.PREFIX):
            assert "SECRET" not in json.dumps(result)
            report["operations"].append(
                {
                    "command": copy.deepcopy(command),
                    "result": copy.deepcopy(result),
                    "published_generation": native_runtime_controls._state(
                        app
                    ).generation,
                    "hold": native_mcp_lifecycle.blocking(app),
                }
            )
            if command["type"].endswith("_readback"):
                assert native_mcp_lifecycle.blocking(app)
                assert (
                    native_runtime_controls._state(app).generation
                    == result["policy_before"]["generation"]
                )
                if (
                    result["action"] == "connect"
                    and result["policy_after"]["generation"] == 2
                ):
                    report["tools_off_denial_pending"] = True
        return result

    def emit(row):
        fault = root / "mcp-fail-publication"
        result = row.get("result") or {}
        if (
            fault.exists()
            and not report.get("publication_failed")
            and result.get("action") == "connect"
            and result.get("kind") == "mcp_lifecycle_" + fault.read_text()
        ):
            report["publication_failed"] = fault.read_text()
            report["failed_generation"] = native_runtime_controls._state(app).generation
            report["failed_hold"] = native_mcp_lifecycle.blocking(app)
            (root / "mcp-publication-failed.json").write_text(
                json.dumps(
                    {
                        "phase": fault.read_text(),
                        "generation": report["failed_generation"],
                        "hold": report["failed_hold"],
                    }
                ),
                encoding="utf8",
            )
            raise OSError("Controlled MCP publication unavailable")
        return actual_emit(row)

    async def approve(owner, name, args, decision):
        assert (
            owner is app
            and name == "mcp__private__private"
            and args == {"value": "registered-proof"}
        )
        assert decision.action == "confirm"
        report["approvals"].append(name)
        return tool_approval.ONCE

    async def exercise(name, args, **kwargs):
        result = await actual_tool(name, args, **kwargs)
        if name == "mcp__private__private":
            report["tool"] = result
            report["tool_generation"] = native_runtime_controls._state(app).generation
            (root / "private-tool-proof.json").write_text(
                json.dumps(
                    {
                        "result": result,
                        "generation": report["tool_generation"],
                        "approvals": report["approvals"],
                    }
                ),
                encoding="utf8",
            )
        return result

    def chunks(stream):
        async def generate():
            if stream.ordinal == 1:
                call = SimpleNamespace(
                    index=0,
                    id="private-mcp-call",
                    type="function",
                    function=SimpleNamespace(
                        name="mcp__private__private",
                        arguments=json.dumps({"value": "registered-proof"}),
                    ),
                )
                yield shared["delta"](tools=[call])
                yield shared["delta"](finish="tool_calls")
            else:
                yield shared["delta"]("CONTROLLED MCP COMPLETE")
                yield shared["delta"](finish="stop")

        return generate()

    def joined():
        assert all(process.poll() is not None for process in processes)
        assert all(not reader.is_alive() for reader in readers)
        assert not manager.servers and not manager._stop_failed and manager._closing

    def exit_app(*args, **kwargs):
        joined()
        report["exit_joined"] = True
        return actual_exit(*args, **kwargs)

    async def release():
        joined()
        report["lease_joined"] = True
        return await actual_release()

    with ExitStack() as stack:
        for owner, name, value in (
            (mcp_client.MCPManager, "connect", real_connect),
            (mcp_client.MCPManager, "reload_configs", real_reload),
            (ttyguard, "popen", spawn),
            (manager, "_build", created),
            (gui_rpc, "async_dispatch", management),
            (rpc, "rpc_emit", emit),
            (tool_approval, "approve_over_rpc", approve),
            (app, "_execute_tool", exercise),
            (shared["Stream"], "__aiter__", chunks),
            (app, "exit", exit_app),
            (app, "_release_owned_storage", release),
        ):
            stack.enter_context(patch.object(owner, name, value))
        try:
            await shared["serve"](app)
        finally:
            report["proc_exited"] = all(
                process.poll() is not None for process in processes
            )
            report["reader_joined"] = all(not reader.is_alive() for reader in readers)
            report["final_enabled"] = app.settings.mcp_enabled
            state = native_runtime_controls._state(app)
            report["final_generation"] = state.generation if state is not None else None
            report["final_hold"] = native_mcp_lifecycle.blocking(app)
            manager.stop_all()
            (root / "mcp-lifecycle.json").write_text(
                json.dumps(report, indent=2), encoding="utf8"
            )


shared["history"].serve = serve
if __name__ == "__main__":
    saved = json.loads((root / "settings.json").read_text(encoding="utf8"))
    saved["mcp_enabled"] = True
    (root / "settings.json").write_text(json.dumps(saved), encoding="utf8")
    report["saved_startup_enabled"] = True
    shared["history"].main()
