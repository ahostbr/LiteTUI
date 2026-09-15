"""Opt-in bounded native command lifecycle probe; no delegation or project edits."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from codex_mcp_turn_probe import gate_command, trust_gate

from litetui.codex_app_server import AppServer
from litetui.codex_runtime import restart_readiness
from litetui.model_transport import credential_path


def gate_program(command, marker, log):
    return (
        "import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n"
        "name=p.get('tool_name'); args=p.get('tool_input') or {}\n"
        "if not isinstance(args,dict): args={}\n"
        f"matched=args.get('cmd',args.get('command'))=={command!r}\n"
        "allow=False\n"
        "is_exec=name in ('exec_command','Bash')\n"
        "if is_exec and matched:\n"
        " try:\n"
        f"  with Path({str(marker)!r}).open('x') as f: f.write('used')\n"
        "  allow=True\n except FileExistsError: pass\n"
        "elif name=='tool_search': allow=True\n"
        f"with Path({str(log)!r}).open('a') as f: f.write(json.dumps({{'is_exec':is_exec,'matched':matched,'allowed':allow}})+'\\n')\n"
        "print(json.dumps({} if allow else {'hookSpecificOutput':{'hookEventName':'PreToolUse',"
        "'permissionDecision':'deny','permissionDecisionReason':'Only the single synthetic sleeper is allowed'}}))\n"
    )


async def probe(output):
    evidence = {"accepted": False, "delegation_enabled": False, "commands": [], "hook_statuses": []}
    with tempfile.TemporaryDirectory(prefix="litetui-native-background-") as directory:
        root = Path(directory)
        sleeper, gate, marker, log = [root / name for name in ("sleep.py", "gate.py", "used", "log")]
        sleeper.write_text("import time\nprint('READY', flush=True)\ntime.sleep(20)\nprint('EXIT', flush=True)\n", encoding="utf-8")
        command = f'{Path(sys.executable).name} "{sleeper}"'
        gate.write_text(gate_program(command, marker, log), encoding="utf-8")
        shutil.copy2(credential_path("codex"), root / "auth.json")
        hook_command = gate_command(gate)
        server = AppServer(config_overrides=["features.hooks=true", "features.multi_agent=false", "features.multi_agent_v2=false",
            'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command='
            + json.dumps(hook_command) + ',timeout=10}]}]'])
        server.environment.update(CODEX_HOME=str(root), PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
        thread = turn = None
        phase = "setup"
        try:
            async with asyncio.timeout(100):
                await server.start()
                evidence["hook_trust"] = await trust_gate(server, root, hook_command)
                thread = (await server.request("thread/start", {
                    "model": "gpt-6-astra", "cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only",
                    "baseInstructions": "This is a synthetic command lifecycle test. Execute only the exact supplied command once with exec_command, yield_time_ms 1000. Immediately reply DONE after the tool yields a running session. Do not poll, wait, inspect files, use write_stdin, or run any other command. Never delegate.",
                }))["thread"]["id"]
                phase = "model_turn"
                turn = (await server.request("turn/start", {"threadId": thread, "effort": "medium",
                    "input": [{"type": "text", "text": "Run this exact command once, yield_time_ms=1000, then immediately reply DONE: " + command}],
                }))["turn"]["id"]
                evidence.update(thread_id=thread, turn_id=turn)
                while True:
                    event = await server.events.get()
                    if isinstance(event, Exception):
                        raise event
                    if "id" in event and "method" in event:
                        await server.send({"id": event["id"], "error": {"code": -32601, "message": "No interactive requests in this probe"}})
                    params = event.get("params", {})
                    if event.get("method") == "hook/completed":
                        status = params.get("run", {}).get("status")
                        evidence["hook_statuses"].append(status if status in ("completed", "blocked", "failed", "stopped") else "other")
                    if event.get("method") in ("item/started", "item/completed"):
                        item = params.get("item") or {}
                        if item.get("type") == "commandExecution":
                            output_text = str(item.get("aggregatedOutput") or "").lower()
                            evidence["commands"].append({
                                "completed_event": event["method"] == "item/completed",
                                "status": item.get("status") if item.get("status") in ("inProgress", "completed", "failed", "declined") else "other",
                                "exit_code": item.get("exitCode"), "process_id_present": bool(item.get("processId")),
                                "ready_marker": "ready" in output_text, "exit_marker": "exit" in output_text,
                                "error_signals": [token for token in ("not found", "not recognized", "permission", "denied", "sandbox", "failed", "cannot", "no such file") if token in output_text],
                            })
                    if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn:
                        evidence["turn_completed"] = params["turn"].get("status") == "completed"
                        turn = None
                        break
                evidence["pending_after_turn"] = len(server.runtime_activity.commands)
                evidence["readiness_after_turn"] = await restart_readiness(server)
                print(json.dumps({"phase": "turn_finished", "pending_commands": evidence["pending_after_turn"]}), flush=True)
                phase = "await_native_exit"
                while server.runtime_activity.commands:
                    event = await asyncio.wait_for(server.events.get(), 35)
                    if isinstance(event, Exception):
                        raise event
                evidence["readiness_after_exit"] = await restart_readiness(server)
                evidence["accepted"] = (evidence["turn_completed"] and evidence["pending_after_turn"] == 1
                    and evidence["readiness_after_turn"] == {"ready": False, "reason": "native_process_pending"}
                    and evidence["readiness_after_exit"]["ready"])
        except Exception as exc:  # noqa: BLE001 - metadata-only probe failure record
            evidence.update(failure_phase=phase, failure_type=type(exc).__name__)
        finally:
            if thread and turn:
                try:
                    await asyncio.wait_for(server.request("turn/interrupt", {"threadId": thread, "turnId": turn}), 5)
                except Exception:  # noqa: BLE001 - process close is still mandatory
                    evidence["interrupt_failed"] = True
            await server.close()
            evidence["app_server_closed"] = server.process is None or server.process.returncode is not None
            evidence["gates"] = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    evidence["temporary_home_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")
    asyncio.run(probe(args.output))
