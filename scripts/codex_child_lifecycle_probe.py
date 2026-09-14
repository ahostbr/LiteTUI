"""One explicitly authorized parent/child protocol probe. Never retry inference."""

import argparse
import asyncio
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from litetui.codex_app_server import AppServer
from litetui.model_transport import credential_path


def gate_program(marker, diagnostics):
    return (
        "import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n"
        "name=p.get('tool_name','')\n"
        "category={'spawn_agent':'spawn','Agent':'spawn','wait':'wait','wait_agent':'wait',"
        "'list_agents':'list','tool_search':'search'}.get(name,'other')\n"
        "allow=category in ('wait','list')\n"
        "if category=='spawn':\n"
        " try:\n"
        f"  with Path({str(marker)!r}).open('x') as f: f.write('1')\n"
        "  allow=True\n except FileExistsError: allow=False\n"
        f"with Path({str(diagnostics)!r}).open('a') as f:\n"
        " f.write(json.dumps({'category':category,'allowed':allow})+'\\n')\n"
        "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse',"
        "'permissionDecision':'allow' if allow else 'deny',"
        "'permissionDecisionReason':'Bounded synthetic protocol probe'}}))\n"
    )


def enum_value(value, allowed):
    return value if isinstance(value, str) and value in allowed else "other"


async def probe(output):
    evidence = {"accepted": False, "sibling_isolation_tested": False}
    with tempfile.TemporaryDirectory(prefix="litetui-one-child-") as directory:
        root = Path(directory)
        shutil.copy2(credential_path("codex"), root / "auth.json")
        hook = root / "gate.py"
        marker = root / "spawn-used"
        diagnostics = root / "gate-counts.jsonl"
        hook.write_text(gate_program(marker, diagnostics), encoding="utf-8")
        overrides = ["features.hooks=true", "features.multi_agent_v2=true",
                     "agents.max_concurrent_threads_per_session=1",
                     "agents.default_subagent_model=\"gpt-6-astra\"",
                     'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command='
                     + json.dumps(f'python "{hook}"') + ',timeout=10}]}]']
        server = AppServer(config_overrides=overrides)
        server.environment["CODEX_HOME"] = str(root)
        parent = parent_turn = child = child_turn = None
        methods = Counter()
        statuses = Counter()
        item_types = Counter()
        hook_statuses = Counter()
        phase = "setup"
        child_notifications = 0
        process = None
        try:
            async with asyncio.timeout(100):
                await server.start()
                listed = await server.request("hooks/list", {"cwds": [str(root)]})
                hooks = []

                def visit(value):
                    if isinstance(value, dict):
                        if "currentHash" in value and str(hook) in value.get("command", ""):
                            hooks.append(value)
                        for nested in value.values():
                            visit(nested)
                    elif isinstance(value, list):
                        for nested in value:
                            visit(nested)

                visit(listed)
                assert hooks, "probe hook unavailable"
                states = ",".join(json.dumps(h["key"]) + "={trusted_hash=" + json.dumps(h["currentHash"]) + "}" for h in hooks)
                await server.close()
                server = AppServer(config_overrides=[*overrides, "hooks.state={" + states + "}"])
                server.environment["CODEX_HOME"] = str(root)
                await server.start()
                process = server.process
                opened = await server.request("thread/start", {
                    "model": "gpt-6-astra", "cwd": str(root), "ephemeral": False,
                    "approvalPolicy": "never", "sandbox": "read-only",
                    "developerInstructions": "This is a bounded synthetic protocol test. No file access, commands, network or external communication. Spawn exactly one child with spawn_agent. Tell the child to write a 4000-word original essay about imaginary geometric shapes, using no tools and no delegation. Wait for that child; if interrupted, accept that as the expected outcome. Do not spawn another child. Finally reply PARENT_OK only.",
                })
                parent = opened["thread"]["id"]
                phase = "parent_start"
                started = await server.request("turn/start", {"threadId": parent, "model": "gpt-6-astra", "effort": "medium",
                    "input": [{"type": "text", "text": "Run the one-child protocol test now."}]})
                parent_turn = started["turn"]["id"]
                phase = "observe_child"
                interrupted = False
                parent_completed = False
                while not parent_completed:
                    event = await asyncio.wait_for(server.events.get(), 30)
                    if isinstance(event, Exception):
                        raise event
                    method, params = event.get("method", ""), event.get("params", {})
                    methods[enum_value(method, {
                        "remoteControl/status/changed", "thread/started", "thread/settings/updated",
                        "thread/status/changed", "thread/tokenUsage/updated", "turn/started",
                        "turn/completed", "item/started", "item/completed", "item/agentMessage/delta",
                        "item/reasoning/summaryTextDelta", "item/reasoning/textDelta",
                        "mcpServer/startupStatus/updated", "hook/started", "hook/completed",
                        "account/rateLimits/updated"})] += 1
                    if "id" in event and method:
                        # No interactive approval or question is part of this test.
                        await server.send({"id": event["id"], "error": {"code": -32601, "message": "Probe does not accept interactive requests"}})
                        continue
                    if child and params.get("threadId") == child:
                        child_notifications += 1
                    item = params.get("item", {})
                    if method in ("item/started", "item/completed"):
                        item_types[enum_value(item.get("type"), {
                            "userMessage", "agentMessage", "reasoning", "plan", "commandExecution",
                            "fileChange", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall",
                            "webSearch", "imageView", "contextCompaction", "toolSearchCall"})] += 1
                    if method == "hook/completed":
                        hook_statuses[enum_value(params.get("run", {}).get("status"), {
                            "completed", "failed", "blocked", "stopped", "running"})] += 1
                    if item.get("type") == "collabAgentToolCall":
                        for state in item.get("agentsStates", {}).values():
                            if isinstance(state, dict):
                                statuses[enum_value(state.get("status"), {
                                    "pendingInit", "running", "interrupted", "completed",
                                    "errored", "shutdown", "notFound"})] += 1
                        receivers = item.get("receiverThreadIds", [])
                        if item.get("tool") == "spawnAgent" and receivers:
                            assert len(receivers) == 1 and child in (None, receivers[0]), "more than one child"
                            child = receivers[0]
                    if child and not interrupted:
                        history = await server.request("thread/read", {"threadId": child, "includeTurns": True})
                        turns = history.get("thread", {}).get("turns", [])
                        active = next((turn for turn in reversed(turns) if turn.get("status") == "inProgress"), None)
                        if active:
                            child_turn = active["id"]
                            phase = "interrupt_child"
                            await server.request("turn/interrupt", {"threadId": child, "turnId": child_turn})
                            interrupted = True
                    if method == "turn/completed" and params.get("threadId") == parent:
                        parent_completed = params.get("turn", {}).get("status") == "completed"
                        if not parent_completed:
                            raise RuntimeError("parent did not complete normally")
                assert child and interrupted, "child active turn not observed/interrupted"
                phase = "verify_child"
                history = await server.request("thread/read", {"threadId": child, "includeTurns": True})
                state = next((t.get("status") for t in history.get("thread", {}).get("turns", []) if t.get("id") == child_turn), None)
                evidence.update(child_count=1, spawn_gate_used=marker.exists(), targeted_interrupt_acknowledged=True,
                                child_turn_status=state, parent_turn_completed=parent_completed,
                                child_notifications=child_notifications, accepted=state == "interrupted")
        except Exception as error:  # noqa: BLE001 - retain only error class in probe evidence
            evidence["failure_type"] = type(error).__name__
            evidence["failure_phase"] = phase
        finally:
            for thread, turn in ((child, child_turn), (parent, parent_turn)):
                if thread and turn:
                    try:
                        await asyncio.wait_for(server.request("turn/interrupt", {"threadId": thread, "turnId": turn}), 5)
                    except Exception:  # noqa: BLE001 - completed turns can reject cleanup interrupts
                        evidence["cleanup_interrupt_rejections"] = evidence.get("cleanup_interrupt_rejections", 0) + 1
            await server.close()
            evidence["process_closed"] = process is None or process.returncode is not None
        evidence["event_counts"] = dict(methods)
        evidence["agent_status_counts"] = dict(statuses)
        evidence["item_type_counts"] = dict(item_types)
        evidence["hook_status_counts"] = dict(hook_statuses)
        evidence["spawn_gate_used"] = marker.exists()
        evidence["child_identity_observed"] = child is not None
        evidence["gate_decisions"] = [json.loads(line) for line in diagnostics.read_text().splitlines()] if diagnostics.exists() else []
    evidence["temporary_home_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(probe(args.output))
