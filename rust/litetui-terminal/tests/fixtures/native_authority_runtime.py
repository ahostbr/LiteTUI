"""Controlled Rust/Python process integration; real authority/store, fake provider."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

root = Path(os.environ["LITETUI_DATA_ROOT"])
home = root / "home"
home.mkdir()
os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
os.environ["LITETUI_NO_HARNESS"] = "1"
os.environ["LITETUI_DISABLE_UPDATE_CHECK"] = "1"

# Supplied by the test package initializer, never part of the product launcher.
source = Path(__import__("litetui").__path__[-1]).parents[1]
sys.path.insert(0, str(source / "tests"))


def emit(event):
    print(json.dumps(event), flush=True)


async def main():
    import httpx
    from _owned_seat import owned_app, release_all

    from litetui import gui_rpc, rpc, seat_authority
    from litetui.claude_tools import ClaudeTools
    from litetui.event_origin import EventOrigin

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Provider/network invocation forbidden in authority fixture")

    app = None
    worker = None
    # Constructor/store/authority/dispatcher are real. External calls are tripwired.
    with patch("platform.node", return_value="controlled-host"), \
            patch("subprocess.Popen", side_effect=forbidden), \
            patch.object(httpx.Client, "send", side_effect=forbidden), \
            patch.object(httpx.AsyncClient, "send", side_effect=forbidden):
        try:
            app = owned_app(rpc=True, tool_profile="strict", initial_backend="claude",
                            initial_model="claude-sonnet-5-5")
            # This fixture bypasses on_mount/provider readiness. Select only its
            # controlled catalog entry before observing the real state read.
            app.model_id = "claude-sonnet-5-5"
            backend = app.backend
            backend.segment_id = 'controlled-segment'
            app._active_tool_profile = "strict"
            app._hook_source = "rpc"
            app._stop_requested = False
            # The fixture task stands in for the provider worker. Stop semantics,
            # approval waiter release and authority cleanup remain source-owned.
            app._chat_running = lambda: worker is not None and not worker.done()
            app._system = lambda *_: None
            app.store.stage(str(uuid4()))
            app.store.convo_dir.mkdir()
            app.store.pending = False
            app.conversation = []
            app._paint_now = lambda: None
            rpc.rpc_emit = emit
            bridge = ClaudeTools(app, backend, backend.segment_id, workspace=Path.cwd())
            assert app.tools_enabled
            assert seat_authority.confirm_route(app) == "host"
            emit({"type": "ready","protocol_version": 1,"launch_status": "ready","backend": "claude",
                      "model": "claude-sonnet-5-5","conversation_id": app.convo_id,
                      "tools_enabled": app.tools_enabled,"mcp_enabled": app.settings.mcp_enabled,
                      "tool_profile": app._active_tool_profile,"tool_profile_requested": app._cli_tool_profile,
                      "approval_route": seat_authority.confirm_route(app),"cwd": os.getcwd(),
                      "management_protocols": [1],"capabilities": list(gui_rpc.OPERATIONS)})
            target = Path.cwd() / "controlled-write.txt"
            turn = 0

            async def execute(call_id, origin):
                def native(event):
                    app._rpc_emit(event, origin=origin)
                args = {"file_path": str(target), "content": "controlled fixture"}
                native({"type": "native_activity","provider": "claude","kind": "tool_use","id": call_id,
                          "name": "Write","complete": True,"input": args,"result": None,"is_error": None,"card_index": 0})
                outcome = await bridge.pre_tool({"tool_name":"Write","tool_input":args},call_id,None)
                allowed = outcome["hookSpecificOutput"]["permissionDecision"] == "allow"
                if allowed:
                    target.write_text("controlled fixture", encoding="utf-8")
                native({"type": "native_activity","provider": "claude","kind": "tool_result","id": call_id,
                          "name": "Write","complete": True,"input": None,"result": "written" if allowed else "denied",
                          "is_error": not allowed,"card_index": 0})
                native({"type": "text_delta","text": "Controlled write allowed" if allowed else "Controlled write denied",
                          "card_index": 0})
                native({"type": "turn_end","stopReason": "cancelled" if app._stop_requested else "stop","error": None})

            while line := await asyncio.to_thread(sys.stdin.readline):
                command = json.loads(line)
                with (root / "all-commands.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(command) + "\n")
                if command["type"].startswith("gui."):
                    # Real management implementation and serialization; only its
                    # unrelated hardware observer is controlled.
                    with patch("litetui.gpu_gate.is_rtx_5090", return_value=False):
                        result = await gui_rpc.async_dispatch(app, command)
                    rpc._respond(command["id"], ok=True, result=result)
                    continue
                with (root / "commands.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(command) + "\n")
                if command["type"] == "shutdown":
                    bridge.stop()
                    if worker is not None:
                        await worker
                    emit({"type": "response","id": command["id"],"ok": True,"result": {"ok":True}})
                    break
                if command["type"] in {"approve", "abort"}:
                    rpc._dispatch(app, command)
                    continue
                assert command["type"] == "prompt" and command["tool_profile"] == "strict"
                if worker is not None:
                    await worker
                turn += 1
                app._stop_requested = False
                app.conversation.append({"role":"user","content":command["message"]})
                emit({"type": "response","id": command["id"],"ok": True,"result": {"turn":"accepted"}})
                app._gui_operation_id = command["id"]
                origin = EventOrigin(session_id=app.convo_id, operation_id=command["id"],
                                     source="rpc").scheduled()
                app._rpc_emit({"type": "turn_start","model": "claude-sonnet-5-5"}, origin=origin)
                worker = asyncio.create_task(execute(f"controlled-{turn}", origin))
            assert not getattr(app, "_approval_waiters", {}) and not getattr(app, "_approval_authority_records", {})
            (root / "authority-store-path.txt").write_text(str(app.store.convo_path), encoding="utf-8")
        finally:
            if worker is not None and not worker.done():
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            try:
                if app is not None:
                    await app.client.close()
            finally:
                release_all()


asyncio.run(main())
