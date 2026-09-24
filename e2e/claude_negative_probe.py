"""Explicit live negative-authority probe using the actual bridge and SDK."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace


async def main():
    from claude_agent_sdk import ClaudeSDKClient

    from litetui.claude_backend import ClaudeBackend
    from litetui.claude_tools import ClaudeTools
    from litetui.settings import Settings

    with tempfile.TemporaryDirectory(prefix="litetui-claude-negative-") as directory:
        root = Path(directory)
        cfg = Settings(backend="claude")
        backend = ClaudeBackend(cfg)
        backend.segment_id = "negative"
        executed = []
        app = SimpleNamespace(backend=backend, convo_id="negative", tools_enabled=False,
            _stop_requested=False, settings=cfg, _active_tool_profile="autonomous", _pending_tool_images=[])
        app._all_tools = lambda: [{"type": "function", "function": {"name": "chrome", "description": "Test sentinel. Call once.",
            "parameters": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}}}]
        async def execute(name, args):
            executed.append((name, args))
            return "EXECUTED", True
        async def authorize(*args, **kwargs):
            return None
        app._execute_tool = execute
        app._authorize_action = authorize
        bridge = ClaudeTools(app, backend, "negative", workspace=root)
        # Register the MCP inventory while on, then turn off before runtime
        # execution: prove denial is not merely hiding tools from the model.
        app.tools_enabled = True
        options = bridge.sdk_options()
        app.tools_enabled = False
        native = root / "MUST-NOT-EXIST.txt"
        options.update(tools=["Write", "Agent"], allowed_tools=["Write", "Agent", "mcp__litetui__chrome"])
        opts = await backend._options(cwd=root, **options)
        calls = []
        original = bridge.pre_tool
        async def record(data, ident, ctx):
            decision = await original(data, ident, ctx)
            calls.append({"name": data.get("tool_name"), "decision": decision["hookSpecificOutput"]["permissionDecision"]})
            return decision
        from claude_agent_sdk import HookMatcher
        opts.hooks = {"PreToolUse": [HookMatcher(hooks=[record], timeout=360)]}
        results = []
        async with ClaudeSDKClient(opts) as client:
            for prompt in (
                f"Use Write to create {native} containing DENIED. If denied, stop.",
                "Call mcp__litetui__chrome with action status exactly once. If denied, stop.",
                "Launch the Agent tool to say hello. If denied, stop.",
            ):
                await client.query(prompt)
                async for message in client.receive_response():
                    if type(message).__name__ == "ResultMessage":
                        results.append({"subtype": message.subtype, "is_error": message.is_error})
        assert not native.exists()
        assert not executed
        names = {c["name"] for c in calls}
        assert {"Write", "mcp__litetui__chrome", "Agent"} <= names, calls
        assert all(c["decision"] == "deny" for c in calls)
        artifact = Path("artifacts/claude-negative-probe.json")
        artifact.write_text(json.dumps({"tool_free_native_and_host_denied": True,
            "agent_launch_denied": True, "host_executions": executed, "calls": calls, "results": results}, indent=2), encoding="utf-8")
        print(f"PASS {artifact}")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1")
    asyncio.run(asyncio.wait_for(main(), 120))
