"""Explicit synthetic live check for the official Codex app-server backend."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

from litetui.codex_app_server import AppServer, AppServerTransport
from litetui.model_transport import collect


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument(
        "--efforts",
        nargs="+",
        default=["low", "medium", "high", "xhigh", "max", "ultra"],
    )
    parser.add_argument("--host-tool", action="store_true")
    parser.add_argument("--deferred-tool", action="store_true")
    parser.add_argument("--compact", action="store_true")
    parser.add_argument("--usage-evidence", type=Path)
    parser.add_argument("--event-evidence", type=Path)
    args = parser.parse_args()
    server = AppServer()
    transport = AppServerTransport(server)
    messages = [
        {
            "role": "system",
            "content": "This is a synthetic connectivity check. Do not call tools or inspect files. Reply OK only.",
        }
    ]
    tools = []
    tool_calls = []
    usage_evidence = []
    rpc_events = []
    if args.host_tool or args.deferred_tool:

        async def execute(name, arguments):
            tool_calls.append(name)
            return "ECHO_OK", True

        app = NS(
            conversation=messages,
            backend=NS(models={}),
            tools_enabled=True,
            _active_tool_profile="scheduled",
            _execute_tool=execute,
            plugins=NS(deferred_specs=list),
            _rpc_emit=rpc_events.append,
            _edit=lambda *args: None,
            _rpc=True,
        )
        transport = AppServerTransport(server, app)
        messages[0]["content"] = (
            "Call litetui_echo once with text OK. Do not inspect files or call any other tools. Then reply OK."
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "echo",
                    "description": "Return a synthetic echo for a connectivity test.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                },
            }
        ]
        if args.deferred_tool:
            deferred = tools
            app.plugins.deferred_specs = lambda: deferred
            tools = []
            messages[0]["content"] = (
                "Use tool search to find the synthetic echo tool in the litetui namespace, "
                "then call it once with text OK. Do not inspect files or call unrelated tools. Then reply OK."
            )
    try:
        for effort in args.efforts:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Find the deferred synthetic echo tool using tool search and call it with text OK, then reply OK."
                        if args.deferred_tool
                        else "Call litetui_echo with text OK, then reply OK."
                        if args.host_tool
                        else "Reply OK only."
                    ),
                }
            )
            start = time.monotonic()
            stream = await transport.create(
                model="gpt-6-astra",
                messages=messages,
                tools=tools,
                extra_body={"reasoning_effort": effort},
                stream=True,
            )

            async def observed(source=stream, level=effort):
                async for chunk in source:
                    usage = getattr(chunk, "usage", None)
                    if usage is not None:
                        usage_evidence.append(
                            {
                                "effort": level,
                                "context_tokens": getattr(
                                    usage, "context_tokens", None
                                ),
                                "latest": getattr(usage, "latest_request_usage", None),
                                "cumulative": getattr(usage, "thread_usage", None),
                                "turn": getattr(usage, "turn_usage", None),
                            }
                        )
                    yield chunk

            response = await collect(observed())
            message = response.choices[0].message
            if (args.host_tool or args.deferred_tool) and "echo" not in tool_calls:
                raise RuntimeError("The requested synthetic host tool was not called.")
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "provider_metadata": message.provider_metadata,
                }
            )
            print(
                json.dumps(
                    {
                        "effort": effort,
                        "completed": True,
                        "seconds": round(time.monotonic() - start, 2),
                        "host_tool_calls": len(tool_calls),
                        **{
                            key: getattr(response.usage, key, None)
                            for key in (
                                "prompt_tokens",
                                "completion_tokens",
                                "cached_tokens",
                                "cache_write_tokens",
                            )
                        },
                    }
                ),
                flush=True,
            )
        if args.compact:
            if transport.app is None:
                transport.app = NS(conversation=messages, tools_enabled=False)
            before = [(m.get("role"), m.get("content")) for m in messages]
            await transport.compact()
            print(
                json.dumps(
                    {
                        "compaction_completed": True,
                        "transcript_preserved": [
                            (m.get("role"), m.get("content")) for m in messages
                        ]
                        == before,
                    }
                ),
                flush=True,
            )
    finally:
        await server.close()
    if args.usage_evidence:
        args.usage_evidence.write_text(
            json.dumps(usage_evidence, indent=2) + "\n", encoding="utf-8"
        )
    if args.event_evidence:
        starts = [e for e in rpc_events if e["type"] == "tool_call"]
        ends = [e for e in rpc_events if e["type"] == "tool_result"]

        def identity(event):
            return event.get("threadId"), event.get("turnId"), event.get("id")

        paired = {identity(e) for e in starts} == {identity(e) for e in ends}
        result = {
            "starts": len(starts),
            "results": len(ends),
            "identities_match": paired,
            "unique_starts": len({identity(e) for e in starts}) == len(starts),
            "unique_results": len({identity(e) for e in ends}) == len(ends),
            "complete_identity": all(all(identity(e)) for e in starts + ends),
            "text_alias_matches": all(e.get("text") == e.get("result") for e in ends),
            "durations_reported": all(
                isinstance(e.get("durationMs"), (int, float)) for e in ends
            ),
        }
        if args.compact:
            compact_starts = [e for e in rpc_events if e["type"] == "compaction_start"]
            compact_ends = [e for e in rpc_events if e["type"] == "compaction_end"]
            result["manual_compaction_pair"] = len(compact_starts) == len(
                compact_ends
            ) == 1 and identity(compact_starts[0]) == identity(compact_ends[0])
            result["manual_compaction_success"] = (
                bool(compact_ends)
                and compact_ends[0].get("outcome") == "success"
                and compact_ends[0].get("will_resume") is False
            )
        assert starts and all(
            v for k, v in result.items() if k not in ("starts", "results")
        ), result
        args.event_evidence.write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
