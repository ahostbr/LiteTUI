"""Bounded native async-question protocol probe with no command execution."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from codex_mcp_turn_probe import gate_command, trust_gate

from litetui.codex_app_server import AppServer
from litetui.model_transport import credential_path


async def saved_answer(server, root, thread, turn, item):
    from litetui import ask_user_question as auq
    from litetui.codex_async_questions import AsyncQuestions
    from litetui.codex_steering import restore_queue
    from litetui.conversation import ConversationRepository

    metadata = {"provider": "codex", "app_server_thread_id": thread}
    events = []
    store = ConversationRepository()
    store.convo_dir, store.convo_path = root / "host", root / "host" / "convo.jsonl"
    store.convo_dir.mkdir()
    app = NS(_rpc=True, is_running=True, _stop_requested=False, convo_id="synthetic",
             conversation=[{"role": "user", "content": "Synthetic question", "provider_metadata": metadata}],
             _pending_input=[], chosen_tool_profile="interactive", store=store, _rpc_emit=events.append)
    app._edit = lambda index, _: store.record_edit(index, app.conversation[index])

    async def execute(name, args):
        assert name == "ask_user_question"
        return await asyncio.to_thread(auq.run, args, app), True

    app._execute_tool = execute
    manager = AsyncQuestions(NS(app=app, server=server, thread_id=thread, turn_id=turn))
    try:
        store.record_msg(app.conversation[0])
        manager.open(item, metadata, 0)
        while not events:
            await asyncio.sleep(0.01)
        assert auq.resolve_over_rpc(app, events[0]["id"], "submit", [{"selected": [0]}])
        while manager.active:
            await asyncio.sleep(0.01)
        assert len(app._pending_input) == 1
        original = app._pending_input[0]["content"]
        _, app.conversation = ConversationRepository.read(store.convo_path)
        app._pending_input = []
        restore_queue(app)
        restore_queue(app)
        assert len(app._pending_input) == 1
        recovered = app._pending_input[0]
        assert recovered["content"] == original and "Answer: Blue" in original
        return recovered["content"]
    finally:
        manager.cancel()
        await asyncio.gather(*(worker for _, worker in manager.active.values()), return_exceptions=True)
        store.release()


async def probe(output, reply=False):
    evidence = {"accepted": False, "async_question_observed": False, "delegation_enabled": False}
    with tempfile.TemporaryDirectory(prefix="litetui-async-question-") as directory:
        root = Path(directory)
        gate = root / "gate.py"
        gate.write_text(
            "import json,sys\np=json.load(sys.stdin)\n"
            "allow=p.get('tool_name') in ('request_user_input_async','tool_search')\n"
            "print(json.dumps({} if allow else {'hookSpecificOutput':{'hookEventName':'PreToolUse',"
            "'permissionDecision':'deny','permissionDecisionReason':'Synthetic question tools only'}}))\n",
            encoding="utf-8")
        shutil.copy2(credential_path("codex"), root / "auth.json")
        command = gate_command(gate)
        server = AppServer(config_overrides=["features.hooks=true", "features.multi_agent=false",
            "features.multi_agent_v2=false", 'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command='
            + json.dumps(command) + ',timeout=10}]}]'])
        server.environment.update(CODEX_HOME=str(root), PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
        thread = turn = None
        question_item = None
        try:
            async with asyncio.timeout(90):
                await server.start()
                evidence["hook_trust"] = await trust_gate(server, root, command)
                thread = (await server.request("thread/start", {"model": "gpt-6-astra", "cwd": str(root),
                    "approvalPolicy": "never", "sandbox": "read-only",
                    "baseInstructions": "Synthetic async question test. Use only request_user_input_async and tool_search. Never execute commands, access files or delegate. Ask the requested question then end the turn; do not infer an answer."}))["thread"]["id"]
                turn = (await server.request("turn/start", {"threadId": thread, "effort": "medium",
                    "input": [{"type": "text", "text": "Use request_user_input_async to ask exactly: Which synthetic color? Options: Blue, Green. Then end the turn without answering the question. If the tool is unavailable, say unavailable and stop."}]}))["turn"]["id"]
                while True:
                    event = await server.events.get()
                    if isinstance(event, Exception):
                        raise event
                    if "id" in event and "method" in event:
                        await server.send({"id": event["id"], "error": {"code": -32601, "message": "Only async questions in this probe"}})
                        continue
                    params = event.get("params") or {}
                    if params.get("threadId") != thread:
                        continue
                    item = params.get("item") or {}
                    if event.get("method") == "item/completed" and item.get("delivery") == "async":
                        question_item = item
                        evidence["async_question_observed"] = True
                        evidence["question_matches"] = item.get("questions") == [{"title": "Which synthetic color?", "options": ["Blue", "Green"]}]
                    if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn:
                        evidence["turn_completed"] = params["turn"].get("status") == "completed"
                        question_turn = turn
                        turn = None
                        break
                evidence["accepted"] = bool(evidence["async_question_observed"] and evidence.get("question_matches") and evidence["turn_completed"])
                if reply:
                    assert evidence["accepted"]
                    evidence["accepted"] = False
                    answer = await saved_answer(server, root, thread, question_turn, question_item)
                    evidence["shared_rpc_answer_reloaded_once"] = True
                    turn = (await server.request("turn/start", {"threadId": thread, "effort": "medium",
                        "input": [{"type": "text", "text": answer + "\nReply exactly BLUE_ACK without tools."}]}))["turn"]["id"]
                    text = ""
                    while True:
                        event = await server.events.get()
                        if isinstance(event, Exception):
                            raise event
                        params = event.get("params") or {}
                        if "id" in event and "method" in event:
                            await server.send({"id": event["id"], "error": {"code": -32601, "message": "No interactive requests"}})
                        if params.get("threadId") != thread:
                            continue
                        if event.get("method") == "item/agentMessage/delta":
                            text += params.get("delta", "")
                        if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn:
                            evidence["reply_completed"] = params["turn"].get("status") == "completed"
                            turn = None
                            break
                    evidence["reply_matches"] = text.strip() == "BLUE_ACK"
                    evidence["accepted"] = evidence["reply_completed"] and evidence["reply_matches"]
        except Exception as exc:  # noqa: BLE001 - fixed metadata only
            evidence["failure_type"] = type(exc).__name__
        finally:
            if thread and turn:
                try:
                    await asyncio.wait_for(server.request("turn/interrupt", {"threadId": thread, "turnId": turn}), 5)
                except Exception:  # noqa: BLE001 - closing owned process is mandatory
                    evidence["interrupt_failed"] = True
            await server.close()
            evidence["app_server_closed"] = server.process is None or server.process.returncode is not None
    evidence["temporary_home_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reply", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")
    asyncio.run(probe(args.output, args.reply))
