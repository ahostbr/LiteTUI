"""Opt-in local CLI-auth SDK contract probe; synthetic prompts only.

Uses official SDK/CLI auth exactly as the LiteSuite adapter does: inherits the
process environment and leaves credentials to the runtime. Never reads tokens.
Run: python e2e/claude_live_probe.py --run-live --output artifacts/claude-p0-live.json
This local verification does not establish permission for product distribution.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.metadata
import json
import tempfile
import uuid
from pathlib import Path


async def probe(root: Path) -> dict:
    import claude_agent_sdk as sdk

    events = []
    hooks = []

    async def deny(tool_input, tool_id, context):
        hooks.append({"tool_name": tool_input.get("tool_name"), "tool_use_id": tool_id})
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "Probe denies all tool execution"}}

    def options(**overrides):
        values = {
            "cwd": str(root), "tools": [], "setting_sources": [], "skills": [],
            "strict_mcp_config": True, "mcp_servers": {}, "permission_mode": "dontAsk",
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": "This is a tiny synthetic LiteTUI integration test. Answer briefly."},
            "include_partial_messages": True, "verbatim_prompts": True,
            "extra_args": {"no-chrome": None, "disable-slash-commands": None, "replay-user-messages": None},
            "hooks": {"PreToolUse": [sdk.HookMatcher(hooks=[deny])]},
        }
        values.update(overrides)
        return sdk.ClaudeAgentOptions(**values)

    async def turn(client, prompt):
        await client.query(prompt)
        text = ""
        result = None
        async for message in client.receive_response():
            data = dataclasses.asdict(message)
            # Synthetic prompts only, but record shapes rather than arbitrary output.
            events.append({"class": type(message).__name__, "keys": sorted(data),
                           "subtype": data.get("subtype"), "session_id": data.get("session_id")})
            if isinstance(message, sdk.AssistantMessage):
                text += "".join(block.text for block in message.content if isinstance(block, sdk.TextBlock))
            if isinstance(message, sdk.ResultMessage):
                result = data
        if result is None or result.get("is_error"):
            raise RuntimeError(f"Unsuccessful result: {result and result.get('subtype')}; {text[:300]}")
        return text, result

    result = {"sdk_version": importlib.metadata.version("claude-agent-sdk"), "auth": "official inherited CLI auth; no credentials accessed"}
    async with sdk.ClaudeSDKClient(options()) as client:
        info = await client.get_server_info() or {}
        result["pid"] = info.get("pid")
        _first, terminal = await turn(client, "Remember the test word ORCHID-739. Reply only READY.")
        session_id = terminal["session_id"]
        second, _ = await turn(client, "What is the test word? Reply only with it.")
        assert "ORCHID-739" in second, second
        result.update(two_turn_context=True, session_id=session_id, result_keys=sorted(terminal))
    async with sdk.ClaudeSDKClient(options(resume=session_id)) as client:
        resumed, terminal = await turn(client, "What is the test word? Reply only with it.")
        assert "ORCHID-739" in resumed and terminal["session_id"] == session_id
        result["exact_resume"] = True
    # Missing identity must refuse rather than silently start a new conversation.
    missing = str(uuid.uuid4())
    try:
        async with sdk.ClaudeSDKClient(options(resume=missing)) as client:
            await turn(client, "Reply only READY.")
    except Exception as exc:  # noqa: BLE001 - capture exact runtime refusal class
        result["missing_session_error"] = type(exc).__name__
    else:
        raise AssertionError("Missing session was silently accepted")
    # Native auto-allow must NOT evade PreToolUse denial.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        async with sdk.ClaudeSDKClient(options(tools=["Write"], allowed_tools=["Write"], permission_mode="default")) as client:
            await turn(client, f"Use Write to create {root / 'DENIED.txt'} containing SENTINEL. Do not use other tools. If denied, stop.")
    assert hooks, "Native denial hook was not invoked"
    assert not (root / "DENIED.txt").exists(), "Denied native Write executed"
    result.update(native_denial=True, hook_calls=hooks, events=events)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.run_live:
        parser.error("--run-live is required; no model call made")
    with tempfile.TemporaryDirectory(prefix="litetui-claude-live-") as temporary:
        result = asyncio.run(asyncio.wait_for(probe(Path(temporary)), timeout=180))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"PASS: {args.output}")


if __name__ == "__main__":
    main()
