"""Opt-in native tool allow/deny probe through the actual host bridge."""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from litetui import tool_policy
from litetui.codex_app_server import AppServer, AppServerTransport


async def probe():
    results = []
    for mode in ("allow", "disabled", "tools-off"):
        events, decisions, hooks = [], [], []

        async def dispatch(app, event, data, hooks=hooks, **kwargs):
            hooks.append({"event": event, "tool": data["tool"], "ok": data.get("ok")})
            return NS(allowed=True, reason="")

        async def authorize(name, args, policy, decisions=decisions):
            decision = tool_policy.evaluate(
                "autonomous", policy, args, Path.cwd(), tool_name=name
            )
            decisions.append({"name": name, "decision": decision.action})
            return None if decision.allowed else ("Synthetic denial", False)

        app = NS(
            tools_enabled=mode != "tools-off",
            _stop_requested=False,
            _rpc=True,
            settings=NS(tools_disabled=["bash"] if mode == "disabled" else []),
            plugins=NS(deferred_specs=list, policy_for=lambda name: None),
            backend=NS(models={}),
            conversation=[],
            _active_tool_profile="autonomous",
            hook_config=object(),
            _authorize_action=authorize,
            _rpc_emit=events.append,
        )
        server = AppServer()
        try:
            transport = AppServerTransport(server, app)
            stream = await transport.create(
                model="gpt-6-astra",
                stream=True,
                extra_body={"reasoning_effort": "low"},
                messages=[
                    {
                        "role": "user",
                        "content": "Run a single native shell command to print LITETUI_POLICY_PROBE. If blocked, do not retry or use another tool; just say blocked.",
                    }
                ],
            )
            with patch("litetui.codex_native_policy.hook_host.dispatch", dispatch):
                async for _ in stream:
                    pass
            command_calls = sum(
                e["type"] == "tool_call" and e["name"] == "command" for e in events
            )
            results.append(
                {
                    "mode": mode,
                    "command_calls": command_calls,
                    "policy_decisions": decisions,
                    "host_hook_events": hooks,
                }
            )
            assert command_calls == (1 if mode == "allow" else 0), results[-1]
            assert bool(decisions) == (mode == "allow"), results[-1]
            if mode == "allow":
                assert [h["event"] for h in hooks] == ["tool_before", "tool_after"], results[-1]
                assert hooks[-1]["ok"] is True, results[-1]
            else:
                assert not hooks, results[-1]
        finally:
            await server.close()
    Path("Docs/Plans/codex-host-parity-evidence/native-policy-probe.json").write_text(
        json.dumps(results, indent=2) + "\n"
    )
    print(json.dumps(results))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(probe())
