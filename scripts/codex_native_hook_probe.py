"""Opt-in synthetic native hook test. Records metadata, never tool payloads."""

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from litetui.codex_app_server import AppServer
from litetui.model_transport import credential_path


async def probe():
    with tempfile.TemporaryDirectory(prefix="litetui-hook-probe-") as folder:
        root = Path(folder)
        # A private probe home preserves the real user's hook configuration.
        auth = credential_path("codex")
        shutil.copy2(auth, root / "auth.json")
        os.environ["CODEX_HOME"] = str(root)
        hook, seen = root / "deny.py", root / "seen.jsonl"
        hook.write_text(
            "import json,sys\nfrom pathlib import Path\np=json.load(sys.stdin)\n"
            f"with Path({str(seen)!r}).open('a') as f: "
            "f.write(json.dumps({'tool_name':p.get('tool_name'),"
            "'event':p.get('hook_event_name')})+'\\n')\n"
            "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse',"
            "'permissionDecision':'deny','permissionDecisionReason':'Synthetic probe denial.'}}))\n"
        )
        command = json.dumps(f'python "{hook}"')
        server = AppServer(
            config_overrides=[
                "features.hooks=true",
                'hooks.PreToolUse=[{matcher=".*",hooks=[{type="command",command='
                + command
                + ",timeout=10}]}]",
            ]
        )
        events = []
        try:
            await server.start()
            listed = await server.request("hooks/list", {"cwds": [str(root)]})
            own_hooks = []

            def visit(value):
                if isinstance(value, dict):
                    if "currentHash" in value and str(hook) in value.get("command", ""):
                        own_hooks.append(value)
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(listed)
            print(
                json.dumps(
                    {
                        "own_hooks": [
                            {
                                k: h.get(k)
                                for k in (
                                    "key",
                                    "currentHash",
                                    "trustStatus",
                                    "enabled",
                                )
                            }
                            for h in own_hooks
                        ]
                    }
                ),
                flush=True,
            )
            if own_hooks:
                overrides = list(server.config_overrides)
                states = ",".join(
                    json.dumps(entry["key"])
                    + "={trusted_hash="
                    + json.dumps(entry["currentHash"])
                    + "}"
                    for entry in own_hooks
                )
                overrides.append("hooks.state={" + states + "}")
                await server.close()
                server = AppServer(config_overrides=overrides)
                await server.start()
                own_hooks.clear()
                visit(await server.request("hooks/list", {"cwds": [str(root)]}))
                print(
                    json.dumps(
                        {
                            "trusted_hooks": [
                                {k: h.get(k) for k in ("key", "trustStatus", "enabled")}
                                for h in own_hooks
                            ]
                        }
                    ),
                    flush=True,
                )
            opened = await server.request(
                "thread/start",
                {
                    "model": "gpt-6-astra",
                    "ephemeral": True,
                    "cwd": str(root),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                },
            )
            tid = opened["thread"]["id"]
            await server.request(
                "turn/start",
                {
                    "threadId": tid,
                    "effort": "low",
                    "input": [
                        {
                            "type": "text",
                            "text_elements": [],
                            "text": "Run a single shell command to print LITETUI_NATIVE_HOOK_PROBE. If blocked, do not retry or use another tool; just say blocked.",
                        }
                    ],
                },
            )
            while True:
                event = await asyncio.wait_for(server.events.get(), 90)
                if isinstance(event, Exception):
                    raise event
                method, payload = event.get("method"), event.get("params", {})
                if "id" in event and method:
                    await server.send(
                        {
                            "id": event["id"],
                            "error": {
                                "code": -32601,
                                "message": "Probe rejects additional requests",
                            },
                        }
                    )
                if method in (
                    "hook/started",
                    "hook/completed",
                    "item/started",
                    "item/completed",
                ):
                    item = payload.get("item", {})
                    events.append(
                        {
                            "method": method,
                            "type": item.get("type"),
                            "status": item.get("status")
                            or payload.get("run", {}).get("status"),
                        }
                    )
                if method == "turn/completed":
                    break
            hits = (
                [json.loads(line) for line in seen.read_text().splitlines()]
                if seen.exists()
                else []
            )
            result = {
                "startup_override": True,
                "hook_hits": hits,
                "events": events,
                "pre_tool_hook_executed": bool(hits),
                "blocked_before_command": bool(hits)
                and any(
                    e["method"] == "hook/completed" and e["status"] == "blocked"
                    for e in events
                )
                and not any(e["type"] == "commandExecution" for e in events),
            }
            Path(
                "Docs/Plans/codex-host-parity-evidence/native-hook-startup-probe.json"
            ).write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result))
        finally:
            await server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(probe())
