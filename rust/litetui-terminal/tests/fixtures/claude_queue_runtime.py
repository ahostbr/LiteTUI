"""Pinned SDK message types, real source owner; no process, credential or model access."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

spec = importlib.util.spec_from_file_location("_claude_queue_controls", Path(__file__).with_name("runtime_controls_runtime.py"))
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)
NATIVE = "12345678-1111-4111-8111-123456789abc"
sessions = []


class Client:
    def __init__(self, options):
        self.options = options
        self.frames = asyncio.Queue()
        self.tasks = []
        self.active = False
        sessions.append(self)

    async def connect(self): pass
    async def get_server_info(self): return {}
    async def get_context_usage(self): return {"maxTokens": 200000, "totalTokens": 120}
    async def set_model(self, model):
        assert model == "claude-fixture"

    async def query(self, prompt):
        assert not self.active, "SDK query was sent while the previous turn was busy"
        self.active = True
        ordinal = len(controls.report.setdefault("claude_queries", [])) + 1
        assert ordinal <= 2, "Native input replay"
        app = controls.history.boundary.app_instance
        controls.report["claude_queries"].append({"text": prompt, "delivery": copy.deepcopy({k:v for k,v in app._claude_active_input.items() if k.startswith("_claude_")}),
            "generation": app.backend.session.lifecycle.generation, "tools_enabled":app.tools_enabled,
            "tools":copy.deepcopy(self.options.tools)})
        await self.frames.put(SystemMessage(subtype="init", data={"session_id": NATIVE}))
        await self.frames.put(AssistantMessage(content=[TextBlock("Controlled Claude response")],
            model="claude-fixture", message_id="msg_"+uuid4().hex, uuid=str(uuid4())))
        async def finish():
            if ordinal == 1:
                # Wait until actual stream ownership and native init have been consumed.
                while app.backend.session.session_id != NATIVE or app._claude_ledger.selected["session_id"] != NATIVE:
                    await asyncio.sleep(0.01)
                controls.mark("stream-active")
                await controls.until_file("release-stream")
            self.active = False
            await self.frames.put(ResultMessage(subtype="success",duration_ms=1,duration_api_ms=1,
                is_error=False,num_turns=1,session_id=NATIVE,uuid=str(uuid4()),
                terminal_reason="completed",result="Controlled Claude response"))
        self.tasks.append(asyncio.create_task(finish()))

    async def interrupt(self):
        controls.report["claude_interrupts"] = controls.report.get("claude_interrupts", 0) + 1
        (controls.root/"release-stream").touch()

    async def receive_messages(self):
        while True: yield await self.frames.get()

    async def disconnect(self):
        (controls.root/"release-stream").touch()
        await asyncio.gather(*self.tasks)
        self.active = False


async def metadata(backend):
    backend.models = {"claude-fixture": {"value": "claude-fixture"}}
    return "ok"


actual_serve = controls.history.serve
async def serve(app):
    try:
        await actual_serve(app)
    finally:
        ledger = getattr(app, "_claude_ledger", None)
        controls.report["claude_ledger"] = None if ledger is None else ledger.document
        controls.report["sdk_retired"] = all(not s.active and all(t.done() for t in s.tasks) for s in sessions)
        (controls.root/"runtime-controls-report.json").write_text(
            json.dumps(controls.report,ensure_ascii=False,indent=2),encoding="utf-8")
controls.history.serve = serve

if __name__ == "__main__":
    import claude_agent_sdk

    from litetui import native_claude_queue, plugins, rpc
    from litetui.background_input import Bridge
    from litetui.claude_backend import ClaudeBackend
    from litetui.conversation import ConversationRepository
    original_emit = rpc.rpc_emit
    def emit(row):
        if (controls.case == "claude-queue-emission" and row.get("type") == "response"
                and (row.get("result") or {}).get("kind") == "midturn_queued"):
            controls.report["queue_emission_failed"] = True
            controls.mark("queue-emission-failed")
            raise OSError("Controlled failure writing exact queue confirmation")
        return original_emit(row)
    original_release = ConversationRepository.release
    original_prepare = native_claude_queue.prepare_input
    def release(repository, *args, **kwargs):
        app = controls.history.boundary.app_instance
        if controls.case in {"claude-queue-quit", "claude-queue-emission"} and app is not None and repository is app.store and repository.owned:
            bridge = getattr(app, "_native_work_bridge", None)
            if bridge is not None and bridge.closing:
                controls.report["queue_recovered"] = Bridge(app).recovered
        return original_release(repository, *args, **kwargs)
    def prepare(*args):
        value = original_prepare(*args)
        if controls.case == "claude-queue-uncertain":
            raise OSError("Controlled failure after durable native preparation")
        return value
    original_register = plugins.register_plugins
    def registered(app, registry, order=None, disabled=frozenset()):
        return original_register(app,registry,order=("litetui.plugins.core_tools","litetui.plugins.convo",
            "litetui.plugins.self_compact","litetui.plugins.misc"),disabled=disabled)
    os.environ["CLAUDE_CONFIG_DIR"] = str(controls.root/"private-claude")
    with patch.object(ClaudeBackend,"ensure_running",metadata), patch.object(claude_agent_sdk,"ClaudeSDKClient",Client), patch.object(plugins,"register_plugins",registered), patch.object(ConversationRepository,"release",release), patch.object(native_claude_queue,"prepare_input",prepare), patch.object(rpc,"rpc_emit",emit):
        controls.history.main()
