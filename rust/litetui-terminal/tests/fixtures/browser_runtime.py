"""Real CLI/acquire/resume/store fixture. Provider, mount and visual observers controlled."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

root = Path(os.environ["LITETUI_DATA_ROOT"])
home = root / "home"
home.mkdir(exist_ok=True)
os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
os.environ["LITETUI_NO_HARNESS"] = "1"
os.environ["LITETUI_DISABLE_UPDATE_CHECK"] = "1"
source = Path(__import__("litetui").__path__[-1]).parents[1]
sys.path.insert(0, str(source / "tests"))


def forbidden(*_args, **_kwargs):
    raise AssertionError("Provider/process/network invocation forbidden in browser fixture")


async def serve(app):
    import uuid

    from litetui import (
        background_input,
        conversation_shutdown,
        gui_rpc,
        native_history,
        restored_input,
        rpc,
        seat_authority,
        store_identity,
    )
    from litetui.conversation import ConversationRepository
    from litetui.event_origin import EventOrigin

    app._system = lambda *_: None
    app._refresh_ctx_label = lambda: None
    app._sync_fleet_identity = lambda: None
    app._render_resumed = lambda *_args, **_kwargs: None
    app._ensure_legacy_store_notice = lambda: None
    app._chat_running = lambda: False
    app._stream = forbidden
    assert app._hold_restored_input
    app.model_id = app._agent_session.authority.model
    app.conversation = [{"role": "system", "content": "Browser fixture system"},
                        {"role": "user", "content": "Current saved conversation"}]
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    current = app.convo_id
    target = None
    display_budget = Path(__file__).with_name("fixture-browser-budget").exists()
    for index in range(52):
        repository = ConversationRepository(agent_session=app._agent_session)
        repository.stage(str(uuid.uuid4()))
        repository.acquire()
        repository.pending = False
        repository.record_meta(app.model_id, app._agent_session.authority.name,
                               app._agent_session.authority.agent_id)
        messages: list[dict[str, object]] = [{"role": "system", "content": "Browser fixture system"},
                    {"role": "user", "content": "Saved browser target" if index == 0 else f"Saved row {index}"}]
        if index == 0:
            target = repository.convo_id
            messages.append({"role": "assistant", "content": "Saved target answer",
                "provider_metadata": {"steering": [{
                    "id": "browser-held", "threadId": "saved-thread", "turnId": "saved-turn",
                    "conversationId": target, "state": "queued", "revision": 1,
                    "item": {"content": "UNSENT browser hold", "source": "queued", "tool_profile": "strict"},
                }]}})
        if index == 0 and display_budget:
            messages[1]["content"] = "x" * 40000
        repository.record_snapshot(messages, store_identity=store_identity.capture(app), store_injected=True)
        repository.write_record({"type": "rename", "name": "Browser target" if index == 0 else f"Saved row {index}"})
        assert not repository.persist_error
        path = repository.convo_path
        repository.release()
        assert path is not None
        os.utime(path, (1000 + index, 1000 + index))
    (root / "browser-identity.json").write_text(json.dumps({"current": current, "target": target,
        "agent": app._agent_session.authority.agent_id}), encoding="utf-8")

    def mark(name):
        (root / name).write_text(app.convo_id, encoding="utf-8")

    async def gate(name):
        mark(name + "-entered")
        while not (root / ("release-" + name)).exists():
            await asyncio.sleep(0.01)

    async def history_wait():
        await gate("history")
        assert app.convo_id == target
        app._append({"role": "assistant", "content": "Source history barrier completed"})
        mark("history-complete")

    async def connection_wait():
        assert gui_rpc.current_operation(app) is not None
        assert app.convo_id == target
        await gate("connect")
        if Path(__file__).with_name("fixture-browser-failure").exists():
            raise OSError("Controlled browser connection failure")
        app._connect_settled = True
        app._gui_connection_success = True
        app._resume_connection_error = None
        app._native_history_worker = app.run_worker(history_wait(), name="native-history", group="native-history", exit_on_error=False)
        native_history.track_history_worker(app, app._native_history_worker)
        app._rpc_emit_model_state()
        app._rpc_emit({"type": "usage", "usage": {"context_tokens": 12, "max_context_tokens": 1000}})
        mark("connect-complete")

    app.connect = lambda: app.run_worker(connection_wait(), name="connect", group="connect", exit_on_error=False)
    app.available_models = [app.model_id]
    app._active_tool_profile = app._cli_tool_profile
    assert app._launch_options.work_lifecycle_version == 1
    app._rpc_ready_sent = True
    rpc.rpc_emit({"type": "ready", "protocol_version": 1, "launch_status": "ready",
        "backend": app.backend.name, "model": app.model_id, "conversation_id": app.convo_id,
        "thinking_level": app.thinking_level, "tools_enabled": app.tools_enabled,
        "mcp_enabled": app.settings.mcp_enabled, "tool_profile": app._active_tool_profile,
        "tool_profile_requested": app._cli_tool_profile, "approval_route": seat_authority.confirm_route(app),
        "cwd": os.getcwd(), "management_protocols": [1], "capabilities": list(gui_rpc.OPERATIONS),
        "history_projection_version": app._launch_options.history_projection_version,
        "work_lifecycle_version": app._launch_options.work_lifecycle_version})
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            with (root / "browser-commands.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(command) + "\n")
            if command["type"] == "shutdown":
                proof = await conversation_shutdown.settle_before_shutdown(app)
                result = {"ok": True}
                if proof is not None:
                    result["conversation_shutdown"] = proof
                work_proof = await background_input.settle_before_shutdown(app)
                if work_proof is not None:
                    result["work_shutdown"] = work_proof
                rpc._respond(command["id"], ok=True, result=result)
                break
            if command["type"].startswith("gui."):
                try:
                    with patch("litetui.gpu_gate.is_rtx_5090", return_value=False):
                        result = await gui_rpc.async_dispatch(app, command)
                    if command["type"] == "gui.conversations.native_open" and result["status"] == "connected":
                        assert (root / "connect-complete").exists() and (root / "history-complete").exists()
                        assert result["worker_settled"] and result["connection_settled"] and result["history_settled"] and result["session_id"] == target
                        assert restored_input.status(app) == {"policy": "hold", "held": 1}
                        mark("open-complete")
                    if command["type"] == "gui.state":
                        assert command["include_conversations"] is False
                        if app.convo_id == target and Path(__file__).with_name("fixture-browser-policy").exists():
                            result["tool_policy"]["tools_enabled"] = True
                    if command["type"] == "gui.restored_input.list":
                        assert (root / "open-complete").exists()
                        (root / "browser-held.json").write_text(json.dumps(result), encoding="utf-8")
                    rpc._respond(command["id"], ok=True, result=result)
                except (ValueError, OSError) as exc:
                    rpc._respond(command["id"], ok=False, error=str(exc))
                continue
            assert command["type"] == "prompt"
            assert app.convo_id == target
            assert (root / "browser-held.json").exists()
            assert restored_input.status(app) == {"policy": "hold", "held": 1}
            assert command["message"] == "UNSENT HUMAN DRAFT"
            app._append({"role": "user", "content": command["message"]})
            origin = EventOrigin(session_id=app.convo_id, operation_id=command["id"], source="rpc").scheduled()
            rpc._respond(command["id"], ok=True, result={"turn": "accepted"})
            app._rpc_emit({"type": "turn_start", "model": app.model_id}, origin=origin)
            app._append({"role": "assistant", "content": "Explicit prompt after verified browser open"})
            app._rpc_emit({"type": "text_delta", "text": "Explicit prompt after verified browser open", "card_index": 0}, origin=origin)
            app._rpc_emit({"type": "turn_end", "stopReason": "stop", "error": None}, origin=origin)
        (root / "browser-proof.json").write_text(json.dumps({"session": app.convo_id,
            "held": restored_input.status(app), "messages": app.conversation}), encoding="utf-8")
    finally:
        await app.client.close()


def run(app, **kwargs):
    assert kwargs == {"headless": True}
    asyncio.run(serve(app))


def main():
    import httpx
    from _owned_seat import control_external_effects, release_all

    from litetui import hook_host
    from litetui.app import LiteTUI

    control_external_effects()
    try:
        with patch("platform.node", return_value="controlled-host"), \
                patch("subprocess.Popen", side_effect=forbidden), \
                patch.object(httpx.Client, "send", side_effect=forbidden), \
                patch.object(httpx.AsyncClient, "send", side_effect=forbidden), \
                patch.object(hook_host, "enter_conversation", lambda *_: None), \
                patch.object(hook_host, "leave_conversation", lambda *_: None), \
                patch.object(LiteTUI, "run", run):
            spec = importlib.util.spec_from_file_location("litetui._actual_resume_cli", source / "src/litetui/cli.py")
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            # Explicitly select the native engine; transport remains tripwired.
            sys.argv.extend(["--codex-engine", "native"])
            module.main()
    finally:
        release_all()


main()
