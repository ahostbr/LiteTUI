"""Real owned host-tool approval and saved rules; all provider/tool effects private."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("_approval_controls", Path(__file__).with_name("runtime_controls_runtime.py"))
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)

class Stream(controls.Stream):
    def __aiter__(self):
        async def chunks():
            if self.ordinal in (1, 3, 5):
                item = NS(index=0, id=f"remember-{self.ordinal}", type="function",
                    function=NS(name="private_approval_tool", arguments=json.dumps({"wider": self.ordinal == 5})))
                yield controls.delta(tools=[item])
                yield controls.delta(finish="tool_calls")
            else:
                yield controls.delta("CONTROLLED REMEMBERED APPROVAL ANSWER")
                yield controls.delta(finish="stop")
        return chunks()

async def completions(resource, **request):
    if not request.get("stream"):
        return NS(choices=[NS(message=NS(content="Private approval title"))])
    controls.report["requests"].append(copy.deepcopy(request))
    assert len(controls.report["requests"]) <= 5
    return Stream(request, len(controls.report["requests"]))

if __name__ == "__main__":
    from litetui import plugins, tool_policy
    original = plugins.register_plugins
    def register(app, registry, order=None, disabled=frozenset()):
        value = original(app, registry, order=("litetui.plugins.core_tools", "litetui.plugins.convo", "litetui.plugins.self_compact", "litetui.plugins.misc"), disabled=disabled)
        def tool(args):
            controls.report.setdefault("private_tool_calls", []).append(copy.deepcopy(args))
            return "PRIVATE IN-MEMORY RESULT"
        policy = tool_policy.ToolPolicy(frozenset({tool_policy.WORKSPACE_WRITE}), "Private fixture callback",
            classify_args=lambda args, workspace: frozenset({tool_policy.PROCESS_EXECUTION}) if args.get("wider") else frozenset())
        registry.add_tool("approval-fixture", {"type":"function","function":{"name":"private_approval_tool",
            "description":"Private in-memory callback","parameters":{"type":"object","properties":{"wider":{"type":"boolean"}},"required":["wider"]}}}, tool, policy=policy)
        return value
    controls.history.completions = completions
    with patch.object(plugins, "register_plugins", register):
        controls.history.main()
