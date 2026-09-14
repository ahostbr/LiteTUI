"""Bounded same-thread MCP inventory test; synthetic tools, no delegation."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from codex_mcp_catalog_probe import FIXTURE

from litetui.codex_app_server import AppServer
from litetui.codex_hook_bridge import own_hooks
from litetui.model_transport import credential_path


def gate_program(log):
    return (
        "import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n"
        "name=p.get('tool_name','')\n"
        "category={'tool_search':'search','mcp__catalog_probe__alpha':'alpha',"
        "'mcp__catalog_probe__beta':'beta'}.get(name,'other')\n"
        "allow=category!='other'\n"
        f"with Path({str(log)!r}).open('a') as f: f.write(json.dumps({{'category':category,'allowed':allow}})+'\\n')\n"
        "print(json.dumps({} if allow else {'hookSpecificOutput':{'hookEventName':'PreToolUse',"
        "'permissionDecision':'deny',"
        "'permissionDecisionReason':'Synthetic MCP inventory probe only'}}))\n"
    )


def gate_command(path):
    # Bare executable name works in both CMD and PowerShell. A quoted executable
    # path is only a string expression in PowerShell without its call operator.
    return f'{Path(sys.executable).name} "{path}"'


def mcp_overrides(fixture, state, counts, revision):
    prefix = "mcp_servers.catalog_probe."
    return (
        prefix + "command=" + json.dumps(sys.executable),
        prefix + "args=" + json.dumps([str(fixture), str(state), str(counts)]),
        prefix + "env.CATALOG_REVISION=" + json.dumps(str(revision)),
    )


def thread_mcp_config(fixture, state, counts, revision):
    return {"mcp_servers": {"catalog_probe": {
        "command": sys.executable, "args": [str(fixture), str(state), str(counts)],
        "env": {"CATALOG_REVISION": str(revision)},
    }}}


async def trust_gate(server, root, command):
    hooks = own_hooks(await server.request("hooks/list", {"cwds": [str(root)]}), command)
    if len(hooks) != 1:
        raise AssertionError("The synthetic gate was not uniquely discovered")
    before = hooks[0].get("trustStatus") == "trusted"
    states = json.dumps(hooks[0]["key"]) + "={trusted_hash=" + json.dumps(hooks[0]["currentHash"]) + "}"
    await server.close()
    server.config_overrides += ("hooks.state={" + states + "}",)
    await server.start()
    hooks = own_hooks(await server.request("hooks/list", {"cwds": [str(root)]}), command)
    if len(hooks) != 1 or hooks[0].get("trustStatus") != "trusted" or not hooks[0].get("enabled"):
        raise AssertionError("The synthetic gate is not trusted and enabled")
    return {"trusted_before": before, "trusted_after": True, "enabled": True}


async def probe(output, refresh):
    evidence = {"accepted": False, "delegation_enabled": False, "refresh": refresh, "turns": []}
    with tempfile.TemporaryDirectory(prefix="litetui-mcp-turn-") as directory:
        root = Path(directory)
        fixture, state, counts, gate, gates = [root / name for name in
                                              ("fixture.py", "state", "counts", "gate.py", "gates")]
        fixture.write_text(FIXTURE, encoding="utf-8")
        gate.write_text(gate_program(gates), encoding="utf-8")
        state.write_text("1", encoding="utf-8")
        shutil.copy2(credential_path("codex"), root / "auth.json")
        def config(revision):
            (root / "config.toml").write_text(
                '[mcp_servers.catalog_probe]\ncommand = ' + json.dumps(sys.executable)
                + '\nargs = ' + json.dumps([str(fixture), str(state), str(counts)])
                + '\n[mcp_servers.catalog_probe.env]\nCATALOG_REVISION = '
                + json.dumps(str(revision)) + '\n', encoding="utf-8")

        if refresh in ("session-restart", "thread-config"):
            (root / "config.toml").write_text("# User configuration remains unchanged.\n", encoding="utf-8")
        else:
            config(1)
        original_config = (root / "config.toml").read_bytes()
        command = gate_command(gate)
        server = AppServer(config_overrides=["features.hooks=true", "features.multi_agent=false",
                                             "features.multi_agent_v2=false",
            'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command='
            + json.dumps(command) + ',timeout=10}]}]'])
        server.environment["CODEX_HOME"] = str(root)
        server.environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
        if refresh == "session-restart":
            server.config_overrides += mcp_overrides(fixture, state, counts, 1)
        thread = turn = None
        phase = "start"
        try:
            async with asyncio.timeout(150):
                await server.start()
                phase = "trust_gate"
                evidence["hook_trust"] = await trust_gate(server, root, command)
                opened = await server.request("thread/start", {
                    "model": "gpt-6-astra", "cwd": str(root), "approvalPolicy": "never", "sandbox": "read-only",
                    "baseInstructions": "You are testing synthetic MCP tools. Use only catalog_probe alpha/beta and tool search. Do not use shell, files, web, agents, or other tools. If tools are unavailable report unavailable and stop. After requested tool calls reply DONE.",
                    **({"config": thread_mcp_config(fixture, state, counts, 1)} if refresh == "thread-config" else {}),
                })
                thread = opened["thread"]["id"]
                evidence["thread_id"] = thread
                if refresh == "thread-config":
                    catalog = await server.request("mcpServerStatus/list", {"threadId": thread, "detail": "toolsAndAuthOnly"})
                    evidence["initial_thread_config_present"] = any(row.get("name") == "catalog_probe"
                                                                    and "alpha" in row.get("tools", {})
                                                                    for row in catalog.get("data", []))
                    if not evidence["initial_thread_config_present"]:
                        raise AssertionError("Thread config did not register the synthetic inventory")
                prompts = ["Find catalog_probe alpha and call it with value 'first'.",
                           "The synthetic MCP inventory has changed. Find catalog_probe beta and call it once, then call catalog_probe alpha with value 7 using its current schema."]
                for index, prompt in enumerate(prompts):
                    phase = f"turn_{index + 1}"
                    if index:
                        state.write_text("2", encoding="utf-8")
                        await asyncio.sleep(.3)
                        if refresh == "config-revision":
                            config(2)
                            await server.request("config/mcpServer/reload", {})
                        elif refresh == "session-restart":
                            phase = "session_restart"
                            await server.close()
                            server.config_overrides = tuple(value for value in server.config_overrides
                                                            if not value.startswith("mcp_servers.catalog_probe."))
                            server.config_overrides += mcp_overrides(fixture, state, counts, 2)
                            await server.start()
                            evidence["resumed_hook_trust"] = await trust_gate(server, root, command)
                            resumed = await server.request("thread/resume", {"threadId": thread})
                            evidence["resumed_thread_same"] = resumed["thread"]["id"] == thread
                            if not evidence["resumed_thread_same"]:
                                raise AssertionError("Native resume changed thread identity")
                            from litetui.codex_history import read

                            saved = await read(server, thread)
                            evidence["prior_turn_visible"] = any(
                                row["id"] == evidence["turns"][0]["turn_id"] for row in saved.get("turns", []))
                            phase = "turn_2"
                        elif refresh == "thread-config":
                            phase = "thread_config"
                            process = server.process
                            resumed = await server.request("thread/resume", {
                                "threadId": thread, "config": thread_mcp_config(fixture, state, counts, 2),
                            })
                            evidence["resumed_thread_same"] = resumed["thread"]["id"] == thread
                            evidence["process_unchanged"] = server.process is process and process.returncode is None
                            if not evidence["resumed_thread_same"]:
                                raise AssertionError("Native resume changed thread identity")
                            phase = "turn_2"
                    started = await server.request("turn/start", {"threadId": thread,
                        "input": [{"type": "text", "text": prompt}], "effort": "medium"})
                    turn = started["turn"]["id"]
                    row = {"turn_id": turn, "completed": False, "mcp_completed": 0, "mcp_items": [], "interactive_requests": 0, "hook_statuses": []}
                    evidence["turns"].append(row)
                    while True:
                        event = await server.events.get()
                        if isinstance(event, Exception):
                            raise event
                        if "id" in event and "method" in event:
                            row["interactive_requests"] += 1
                            await server.send({"id": event["id"], "error": {
                                "code": -32601, "message": "Interactive requests are unavailable in this probe"}})
                            continue
                        params = event.get("params", {})
                        if event.get("method") == "hook/completed":
                            status = params.get("run", {}).get("status")
                            row["hook_statuses"].append(status if status in ("completed", "failed", "blocked", "stopped") else "other")
                        if params.get("threadId") != thread:
                            continue
                        if event.get("method") == "thread/tokenUsage/updated":
                            row["usage"] = params.get("tokenUsage")
                        if event.get("method") == "item/completed" and params.get("item", {}).get("type") == "mcpToolCall":
                            row["mcp_completed"] += 1
                            item = params["item"]
                            error = str((item.get("error") or {}).get("message", "")).lower()
                            row["mcp_items"].append({
                                "status": item.get("status") if item.get("status") in ("completed", "failed", "inProgress") else "other",
                                "read_only_hint": item.get("readOnlyHint"), "has_error": bool(error),
                                "error_signals": [token for token in ("approval", "permission", "denied", "unknown", "not found", "schema", "timeout", "hook") if token in error],
                            })
                        if event.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn:
                            row["completed"] = params["turn"].get("status") == "completed"
                            turn = None
                            break
                    if not row["completed"]:
                        break
                call_log = counts.with_suffix(".calls")
                evidence["calls"] = [json.loads(line) for line in call_log.read_text().splitlines()] if call_log.exists() else []
                expected = {(1, "alpha"), (2, "alpha"), (2, "beta")}
                evidence["accepted"] = (len(evidence["turns"]) == 2
                    and all(row["completed"] for row in evidence["turns"])
                    and expected <= {(row["version"], row["tool"]) for row in evidence["calls"] if row["valid"]}
                    and all(row["valid"] for row in evidence["calls"]))
        except Exception as exc:  # noqa: BLE001 - preserve only an enumerated phase and error class
            evidence.update(failure_phase=phase, failure_type=type(exc).__name__)
        finally:
            if thread and turn:
                try:
                    await asyncio.wait_for(server.request("turn/interrupt", {"threadId": thread, "turnId": turn}), 5)
                except Exception:  # noqa: BLE001 - cleanup still closes the process
                    evidence["interrupt_failed"] = True
            await server.close()
            evidence["app_server_closed"] = server.process is None or server.process.returncode is not None
            evidence["gates"] = [json.loads(line) for line in gates.read_text().splitlines()] if gates.exists() else []
            evidence["tools_list_versions"] = [int(line) for line in counts.read_text().splitlines()] if counts.exists() else []
            evidence["config_file_unchanged"] = (root / "config.toml").read_bytes() == original_config
    evidence["temporary_home_removed"] = not root.exists()
    evidence["inventory_accepted"] = evidence["accepted"]
    statuses = [status for row in evidence["turns"] for status in row.get("hook_statuses", [])]
    evidence["hook_accepted"] = (len(evidence["gates"]) >= 3 and len(statuses) >= 3
                                 and all(status == "completed" for status in statuses)
                                 and all(gate["allowed"] for gate in evidence["gates"]))
    evidence["accepted"] = (evidence["inventory_accepted"] and evidence["hook_accepted"]
                            and evidence["app_server_closed"] and evidence["temporary_home_removed"])
    if refresh == "session-restart":
        evidence["accepted"] = (evidence["accepted"] and evidence.get("resumed_thread_same")
                                and evidence.get("prior_turn_visible") and evidence["config_file_unchanged"])
    if refresh == "thread-config":
        evidence["accepted"] = (evidence["accepted"] and evidence.get("resumed_thread_same")
                                and evidence.get("process_unchanged") and evidence["config_file_unchanged"])
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--refresh", choices=("notification", "config-revision", "session-restart", "thread-config"), default="notification")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required for the bounded two-turn inference probe")
    asyncio.run(probe(args.output, args.refresh))
