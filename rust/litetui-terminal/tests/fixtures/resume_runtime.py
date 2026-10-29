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
held_fixture = Path(__file__).with_name("fixture-held-input").exists()
continue_fixture = Path(__file__).with_name("fixture-continue-input").exists()
reconcile_fixture = Path(__file__).with_name("fixture-reconcile-input").exists()
claude_fixture = Path(__file__).with_name("fixture-claude-input").exists()
sys.path.insert(0, str(source / "tests"))


def forbidden(*_args, **_kwargs):
    raise AssertionError("Provider/process/network invocation forbidden in resume fixture")


async def serve(app):
    from litetui import (
        codex_steering,
        gui_rpc,
        restored_input,
        rpc,
        seat_authority,
        store_identity,
    )
    from litetui.event_origin import EventOrigin

    # Visual/fleet/hook observers are not the subject; CLI, session acquisition,
    # _resume_cli_conversation, _resume, settings adoption and store are real.
    app._system = lambda *_: None
    app._refresh_ctx_label = lambda: None
    app._sync_fleet_identity = lambda: None
    app._render_resumed = lambda *_args, **_kwargs: None
    app._ensure_legacy_store_notice = lambda: None
    app._chat_running = lambda: False
    assert app._hold_restored_input, "CLI did not apply the requested queue hold"
    app.model_id = app._agent_session.authority.model
    if app._cli_convo_id:
        assert app._resume_cli_conversation(), app._cli_launch_error
        assert app.thinking_level == "high", "resume replaced saved thinking"
    else:
        app.conversation = [{"role": "system", "content": "Controlled fixture system prompt"}]
        app._prompt_store_identity = store_identity.capture(app)
        app._store_injected = True
    reconciliation_count = 0
    if reconcile_fixture:
        from types import SimpleNamespace

        async def history_start():
            pass

        async def history_request(method, params):
            assert method in ("thread/read", "thread/turns/list"), "Reconciliation attempted a provider mutation"
            with (root / "history-requests.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"method": method, "params": params}) + "\n")
            if method == "thread/read":
                return {"thread": {"id": "held-thread", "historyMode": "paginated"}}
            return {"data": [{"id": "held-turn", "items": (
                [{"type": "userMessage", "clientId": "saved-sending"}] if reconciliation_count > 1 else [])}],
                    "nextCursor": None}
        app.backend.app_server = SimpleNamespace(start=history_start, request=history_request)
        app._stream = forbidden

    continuation_count = 0
    continuation_worker = None
    retire = asyncio.Event()

    def stream(*, origin):
        nonlocal continuation_worker
        from types import SimpleNamespace

        captured = origin.scheduled()
        async def execute():
            app._rpc_emit({"type": "turn_start", "model": app.model_id}, origin=captured)
            answer = "Controlled continued reply"
            app._append({"role": "assistant", "content": answer})
            app._rpc_emit({"type": "text_delta", "text": answer, "card_index": 0}, origin=captured)
            app._rpc_emit({"type": "turn_end", "stopReason": "stop", "error": None}, origin=captured)
            # Deliberately keep the worker alive after turn_end. Only the
            # separate source settlement request releases this fixture barrier.
            await retire.wait()
            (root / "queue-continue-proof.json").write_text(json.dumps({
                "operation": captured.operation_id, "source": captured.source,
                "profile": app._active_tool_profile,
                "delivered": [m for m in app.conversation if m.get("codex_delivery")],
            }), encoding="utf-8")
        task = asyncio.create_task(execute())
        continuation_worker = task
        async def wait():
            await task
        return SimpleNamespace(wait=wait)

    if continue_fixture:
        app._stream = stream
        app._chat_running = lambda: continuation_worker is not None and not continuation_worker.done()

    async def check_held():
        if claude_fixture and app._cli_convo_id:
            import copy

            from litetui import claude_turn

            before = copy.deepcopy([item["_claude_entry"] for item in app._pending_input])
            count = 2 if (root / "queue-cancel-proof.json").exists() else 3
            assert len(before) == count
            evidence = app._claude_ledger.file.read_bytes()
            app._flush_pending_input()
            assert all(not claude_turn.queue_ready(app, item) for item in app._pending_input)
            assert not app._deliver_queued_input()
            claude_turn.command(app, "continue")
            assert [item["_claude_entry"] for item in app._pending_input] == before
            assert app._claude_ledger.file.read_bytes() == evidence
            (root / "held-input-proof.json").write_text(json.dumps({
                "policy": restored_input.status(app), "entries": before,
                "explicit_prompts": [m["content"] for m in app.conversation if m["role"] == "user"],
                "journal": app._claude_ledger.document,
            }), encoding="utf-8")
            return
        if not (held_fixture and app._cli_convo_id):
            return
        import copy
        from types import SimpleNamespace

        before = copy.deepcopy([item["_codex_entry"] for item in app._pending_input])
        count = 2 if any((root / name).exists() for name in ("queue-cancel-proof.json", "queue-continue-proof.json", "queue-reconcile-proof.json")) else 3
        assert len(before) == count and hasattr(app.backend, "app_server")
        assert restored_input.status(app) == {"policy": "hold", "held": count}
        async def denied(*_args, **_kwargs):
            forbidden()
        server = SimpleNamespace(start=denied, request=denied)
        app._flush_pending_input()
        steering = codex_steering.HostSteering(
            app, server, "held-thread", "held-turn", {"steering": before}, forbidden)
        with patch.object(steering, "admit", denied):
            await steering.run()
        assert not await codex_steering.recover_queue_head(app, server)
        after = [item["_codex_entry"] for item in app._pending_input]
        assert after == before, "held input changed delivery evidence"
        (root / "held-input-proof.json").write_text(json.dumps({
            "policy": restored_input.status(app), "entries": after,
            "explicit_prompts": [m["content"] for m in app.conversation if m["role"] == "user"],
        }), encoding="utf-8")

    await check_held()
    app.available_models = [app.model_id]
    app._active_tool_profile = app._cli_tool_profile
    app._rpc_ready_sent = True
    rpc.rpc_emit({"type": "ready", "protocol_version": 1, "launch_status": "ready",
        "backend": app.backend.name, "model": app.model_id, "conversation_id": app.convo_id,
        "thinking_level": app.thinking_level, "tools_enabled": app.tools_enabled,
        "mcp_enabled": app.settings.mcp_enabled, "tool_profile": app._active_tool_profile,
        "tool_profile_requested": app._cli_tool_profile, "approval_route": seat_authority.confirm_route(app),
        "cwd": os.getcwd(), "management_protocols": [1], "capabilities": list(gui_rpc.OPERATIONS)})
    with (root / "launches.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps({"session": app.convo_id, "agent": app._agent_session.authority.agent_id,
                             "thinking": app.thinking_level, "argv": sys.argv,
                             "held": restored_input.status(app)}) + "\n")
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            with (root / "resume-commands.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(command) + "\n")
            if command["type"] == "shutdown":
                rpc._respond(command["id"], ok=True, result={"ok": True})
                break
            if command["type"] == "gui.conversations.read" and (root / "hold-history").exists():
                continue
            if command["type"].startswith("gui."):
                if reconcile_fixture and command["type"] == "gui.restored_input.reconcile":
                    reconciliation_count += 1
                if (continue_fixture and command["type"] == "gui.restored_input.settled"
                        and continuation_worker is not None):
                    assert not continuation_worker.done(), "settlement did not wait for worker retirement"
                    retire.set()
                if continue_fixture and command["type"] == "gui.restored_input.continue":
                    continuation_count += 1
                    with patch.object(seat_authority, "floor_refusal",
                                      return_value="controlled floor refusal" if continuation_count == 1 else None):
                        result = await gui_rpc.async_dispatch(app, command)
                    with (root / "continue-receipts.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps(result) + "\n")
                else:
                    with patch("litetui.gpu_gate.is_rtx_5090", return_value=False):
                        result = await gui_rpc.async_dispatch(app, command)
                if reconcile_fixture and command["type"] == "gui.restored_input.reconcile":
                    with (root / "reconcile-receipts.jsonl").open("a", encoding="utf-8") as log:
                        log.write(json.dumps(result) + "\n")
                    if result["reconciled"]:
                        (root / "queue-reconcile-proof.json").write_text(json.dumps(result), encoding="utf-8")
                if command["type"] == "gui.restored_input.cancel":
                    (root / "queue-cancel-proof.json").write_text(json.dumps(result), encoding="utf-8")
                rpc._respond(command["id"], ok=True, result=result)
                continue
            assert command["type"] == "prompt"
            previous = [m["content"] for m in app.conversation if m["role"] == "user"]
            app._materialise_convo()
            app._append({"role": "user", "content": command["message"]})
            origin = EventOrigin(session_id=app.convo_id, operation_id=command["id"], source="rpc").scheduled()
            rpc._respond(command["id"], ok=True, result={"turn": "accepted"})
            app._rpc_emit({"type": "turn_start", "model": app.model_id}, origin=origin)
            await check_held()
            answer = "Earlier: " + " | ".join(previous) if previous else "Saved first reply"
            record: dict[str, object] = {"role": "assistant", "content": answer}
            if held_fixture and not app._cli_convo_id:
                record["provider_metadata"] = {"steering": [
                    {"id": "saved-" + state, "threadId": "held-thread", "turnId": "held-turn",
                     "conversationId": app.convo_id, "state": state, "revision": 7,
                     "item": {"content": "UNSENT " + state, "source": "queued", "tool_profile": "strict"}}
                    for state in ("queued", "sending", "accepted")
                ]}
            app._append(record)
            if claude_fixture and not app._cli_convo_id:
                from litetui import claude_turn

                ledger = claude_turn.ledger_for(app)
                segment = ledger.select_segment(claude_turn.launch_workspace(app))
                ledger.fix_system_prompt(segment["id"], "Controlled fixture system prompt",
                                         store_identity=store_identity.capture(app), store_injected=True)
                ledger.bind_session(segment["id"], "controlled-native-session")
                for state in ("prepared", "submitted", "acknowledged"):
                    entry = ledger.prepare(segment["id"], "UNSENT " + state, "strict", "harness")
                    if state != "prepared":
                        ledger.update_delivery(entry["id"], "submitted")
                    if state == "acknowledged":
                        ledger.update_delivery(entry["id"], "acknowledged")
            app._rpc_emit({"type": "text_delta", "text": answer, "card_index": 0}, origin=origin)
            app.thinking_level = "high"
            app._rpc_emit({"type": "turn_end", "stopReason": "stop", "error": None}, origin=origin)
        (root / "resume-transcript-path.txt").write_text(str(app.store.convo_path), encoding="utf-8")
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
            if held_fixture:
                # Explicitly select the native engine; transport remains tripwired.
                sys.argv.extend(["--codex-engine", "native"])
            module.main()
    finally:
        release_all()


main()
