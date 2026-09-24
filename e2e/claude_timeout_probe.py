"""Opt-in live runtime gate: an unanswered native approval expires closed."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


async def main():
    from claude_agent_sdk import ClaudeSDKClient

    from litetui.claude_backend import ClaudeBackend
    from litetui.claude_tools import ClaudeTools
    from litetui.settings import Settings

    with tempfile.TemporaryDirectory(prefix="litetui-claude-timeout-") as directory:
        root = Path(directory)
        target = root / "MUST-NOT-EXIST.txt"
        cfg = Settings(backend="claude", tools_enabled=True)
        backend = ClaudeBackend(cfg)
        backend.segment_id = "timeout"
        requested = []
        cancelled = []
        app = SimpleNamespace(backend=backend, convo_id="timeout", tools_enabled=True,
            _stop_requested=False, settings=cfg, _active_tool_profile="strict")
        app._all_tools = list

        async def authorize(*args, **kwargs):
            requested.append(True)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        app._authorize_action = authorize
        bridge = ClaudeTools(app, backend, "timeout", workspace=root)
        # Same production path, accelerated deadline; test the SDK hook response,
        # not a five-minute human wait. Native autoallow must not bypass it.
        with patch("litetui.claude_tools._deadlines", return_value=(0.2, 2.0)):
            options = bridge.sdk_options()
            options.update(tools=["Write"], allowed_tools=["Write"])
            opts = await backend._options(cwd=root, **options)
            results = []
            async with ClaudeSDKClient(opts) as client:
                await client.query(f"Use Write to create {target} containing BAD. If denied, stop; do not retry.")
                async for message in client.receive_response():
                    if type(message).__name__ == "ResultMessage":
                        results.append({"subtype": message.subtype, "is_error": message.is_error})
        assert requested and len(cancelled) == len(requested)
        assert not target.exists()
        assert not bridge._decisions
        assert results and not results[-1]["is_error"]
        output = Path(__file__).resolve().parents[1] / "artifacts" / "claude-timeout-probe.json"
        output.write_text(json.dumps({"unanswered_native_approval_denied": True,
            "owned_decisions_cancelled": len(cancelled), "file_created": False,
            "decision_deadline_seconds": 0.2, "hook_deadline_seconds": 2,
            "limitation": "Accelerated local deadline; production 315s/360s duration not waited out.",
            "results": results}, indent=2), encoding="utf-8")
        print(f"PASS {output}")


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1")
    asyncio.run(asyncio.wait_for(main(), 120))
