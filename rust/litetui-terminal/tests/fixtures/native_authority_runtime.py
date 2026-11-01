"""Controlled Rust/Python process integration; real authority/store, fake provider."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

MODE = "authority"
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
    from litetui.launch_options import LaunchOptions

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
            # This fixture bypasses CLI construction but preserves the actual
            # native launch negotiation before the owned app captures history.
            projection = int(sys.argv[sys.argv.index("--history-projection-version") + 1])
            app = owned_app(rpc=True, tool_profile="strict", initial_backend="claude",
                            initial_model="claude-sonnet-5-5",
                            hold_restored_input="--hold-restored-input" in sys.argv,
                            launch_options=LaunchOptions(history_projection_version=projection))
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
            app.store.record_meta(app.model_id)
            app._paint_now = lambda: None
            if MODE.startswith("controls"):
                app.available_models = ["claude-sonnet-5-5", "claude-opus-5"]
                backend.models = {model: {"resolvedModel": model, "supportedEffortLevels": ["low", "high"]}
                                  for model in app.available_models}
                # Controlled UI/model-info boundaries; selection, persistence,
                # source RPC serialization and authority setters stay real.
                app.update_header = lambda: None
                app.fetch_context_window = lambda: None
                app.apply_context_length = lambda: None
                app.system_message = lambda *_: None
                app._rpc_ready_sent = True
                if MODE == "controls_refuse":
                    remember = app._remember_for_this_convo
                    def rejected(field, value):
                        if field in {"thinking_level", "reasoning_effort"}:
                            raise OSError("controlled owned-write refusal")
                        return remember(field, value)
                    app._remember_for_this_convo = rejected
            rpc.rpc_emit = emit
            bridge = ClaudeTools(app, backend, backend.segment_id, workspace=Path.cwd())
            assert app.tools_enabled
            assert seat_authority.confirm_route(app) == "host"
            emit({"type": "ready","protocol_version": 1,"launch_status": "ready","backend": "claude",
                      "model": "claude-sonnet-5-5","conversation_id": app.convo_id,
                      "tools_enabled": app.tools_enabled,"mcp_enabled": app.settings.mcp_enabled,
                      "tool_profile": app._active_tool_profile,"tool_profile_requested": app._cli_tool_profile,
                      "approval_route": seat_authority.confirm_route(app),"cwd": os.getcwd(),
                      "management_protocols": [1],"capabilities": list(gui_rpc.OPERATIONS),
                      "history_projection_version": app._launch_options.history_projection_version})
            target = Path.cwd() / "controlled-write.txt"
            turn = 0

            async def execute(call_id, origin):
                def native(event):
                    app._rpc_emit(event, origin=origin)
                if MODE.startswith("controls"):
                    native({"type": "text_delta", "card_index": 0,
                            "text": app.model_id + " / " + (app.thinking_level or "default")})
                    native({"type": "turn_end", "stopReason": "stop", "error": None})
                    return
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
                    try:
                        with patch("litetui.gpu_gate.is_rtx_5090", return_value=False):
                            result = await gui_rpc.async_dispatch(app, command)
                    except Exception as exc:  # noqa: BLE001 - mirror the RPC command error boundary
                        rpc._respond(command["id"], ok=False, error=str(exc))
                    else:
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
                app._rpc_emit({"type": "turn_start","model": app.model_id}, origin=origin)
                worker = asyncio.create_task(execute(f"controlled-{turn}", origin))
            assert not getattr(app, "_approval_waiters", {}) and not getattr(app, "_approval_authority_records", {})
            if MODE.startswith("controls"):
                from litetui.agent_ownership import SETTINGS_NAME
                (root / "control-settings-path.txt").write_text(
                    str(app._agent_session._agent.directory / SETTINGS_NAME), encoding="utf-8")
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
