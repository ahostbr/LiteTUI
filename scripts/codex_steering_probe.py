"""Live synthetic steering and persisted client identity capability check."""

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

from litetui.codex_app_server import AppServer, AppServerTransport
from litetui.model_transport import ProviderError, collect


async def main(host_queue=False):
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(name, arguments):
        entered.set()
        await release.wait()
        return "OK", True

    messages = [
        {
            "role": "user",
            "content": "Call litetui_echo once then reply OK. Do not call any other tools.",
        }
    ]
    app = NS(
        conversation=messages,
        backend=NS(models={}),
        tools_enabled=True,
        _active_tool_profile="scheduled",
        _execute_tool=execute,
        plugins=NS(deferred_specs=list),
        _rpc_emit=lambda e: None,
        _edit=lambda *a: None,
        _rpc=True,
    )
    if host_queue:
        app._pending_input = []
        app._append = messages.append
        app.settings = NS(tool_policy_profile="scheduled")
        app.convo_id = "synthetic"
    server = AppServer()
    transport = AppServerTransport(server, app)
    client_id = str(uuid.uuid4())
    task = None
    try:
        stream = await transport.create(
            model="gpt-6-astra",
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "echo",
                        "description": "Return synthetic OK",
                        "parameters": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            extra_body={"reasoning_effort": "low"},
            stream=True,
        )
        task = asyncio.create_task(collect(stream))
        await asyncio.wait_for(entered.wait(), 60)
        turn_id = transport.turn_id
        params = {
            "threadId": transport.thread_id,
            "expectedTurnId": turn_id,
            "clientUserMessageId": client_id,
            "input": [
                {
                    "type": "text",
                    "text": "After the current tool finishes, reply STEER_OK only. Do not call another tool.",
                }
            ],
        }
        if host_queue:
            queued = {
                "content": params["input"][0]["text"],
                "source": "queued",
                "tool_profile": "scheduled",
            }
            app._pending_input.append(queued)

            async def wait_for_delivery():
                while app._pending_input:
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(wait_for_delivery(), 30)
            entry = queued["_codex_entry"]
            assert entry["state"] == "accepted"
            client_id = entry["id"]
            params["clientUserMessageId"] = client_id
            accepted = {"turnId": entry["turnId"]}
        else:
            accepted = await server.request("turn/steer", params)
        release.set()
        response = await asyncio.wait_for(task, 90)
        history = await server.request(
            "thread/read", {"threadId": transport.thread_id, "includeTurns": True}
        )
        matches = [
            item
            for turn in history["thread"]["turns"]
            for item in turn["items"]
            if item.get("type") == "userMessage" and item.get("clientId") == client_id
        ]
        rejected_after_completion = False
        try:
            await server.request("turn/steer", params)
        except ProviderError:
            rejected_after_completion = True
        result = {
            "accepted_same_turn": accepted.get("turnId") == turn_id,
            "persisted_client_id_matches": len(matches),
            "steering_applied": "STEER_OK"
            in (response.choices[0].message.content or ""),
            "completed_turn_rejects_steer": rejected_after_completion,
        }
        assert result == {
            "accepted_same_turn": True,
            "persisted_client_id_matches": 1,
            "steering_applied": True,
            "completed_turn_rejects_steer": True,
        }, result
        path = "host-queue-steering-probe.json" if host_queue else "steering-probe.json"
        Path("Docs/Plans/codex-host-parity-evidence", path).write_text(
            json.dumps(result, indent=2) + "\n"
        )
        print(json.dumps(result))
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--host-queue", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.host_queue))
