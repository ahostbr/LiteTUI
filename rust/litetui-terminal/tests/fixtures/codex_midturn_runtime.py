"""Actual Codex source transport with controlled JSON-RPC; no external process or login."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "_codex_midturn_controls", Path(__file__).with_name("runtime_controls_runtime.py")
)
controls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controls)
THREAD = "controlled-midturn-thread"
servers = []
helpers = []


async def start(server):
    if server.process is None:
        server.process = SimpleNamespace(returncode=None)
        server.native_bridge = SimpleNamespace(policy=None)
        server.session.ready(server.session.begin())
        servers.append(server)
    server.initialized = True


async def finish(server, turn):
    if turn == "controlled-turn-1":
        app = controls.history.boundary.app_instance
        while getattr(app, "_native_work_provider_lane", None) is None:
            await asyncio.sleep(0.01)
        controls.mark("stream-active")
        await controls.until_file("release-stream")
    await server.events.put(
        {
            "method": "turn/completed",
            "params": {"threadId": THREAD, "turn": {"id": turn, "status": "completed"}},
        }
    )


async def request(server, method, params):
    from litetui import background_input as work
    from litetui import native_claude_queue
    from litetui.codex_steering import RejectedRequest
    from litetui.model_transport import ProviderError

    app = controls.history.boundary.app_instance
    controls.report.setdefault("codex_calls", []).append(
        {"method": method, "params": copy.deepcopy(params)}
    )
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        return {
            "data": [
                {
                    "model": "gpt-6.1-sol",
                    "displayName": "Controlled Codex",
                    "defaultReasoningEffort": "high",
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                }
            ]
        }
    if method in {"thread/start", "thread/resume"}:
        return {"thread": {"id": THREAD, "turns": []}}
    if method == "thread/read":
        return {"thread": {"id": THREAD, "turns": []}}
    if method == "turn/start":
        turns = sum(c["method"] == "turn/start" for c in controls.report["codex_calls"])
        assert turns <= 2, "Unexpected native input replay"
        turn = "controlled-turn-" + str(turns)
        await server.events.put(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": THREAD,
                    "turnId": turn,
                    "itemId": turn + "-answer",
                    "delta": "Controlled native answer",
                },
            }
        )
        helpers.append(asyncio.create_task(finish(server, turn)))
        return {"turn": {"id": turn}}
    if method == "turn/steer":
        assert not native_claude_queue.blocking(app)
        entry = app._pending_input[0]["_codex_entry"]
        assert (
            entry["state"] == "sending"
            and entry["work_input_id"] in work.bridge(app).inputs
        )
        controls.mark("native-steer", delivery=copy.deepcopy(entry))
        if controls.case == "codex-midturn-rejected":
            raise RejectedRequest("Controlled pre-injection rejection", -32602)
        if controls.case == "codex-midturn-uncertain":

            async def retained():
                async with asyncio.timeout(10):
                    while not any(
                        i.state == "uncertain" for i in work.bridge(app).inputs.values()
                    ):
                        await asyncio.sleep(0.01)
                    controls.mark("native-uncertain")

            helpers.append(asyncio.create_task(retained()))
            raise ProviderError("Controlled reply loss")
        return {"turnId": "controlled-turn-1"}
    if method == "turn/interrupt":
        assert params == {"threadId": THREAD, "turnId": "controlled-turn-1"}
        (controls.root / "release-stream").touch()
        return {}
    raise AssertionError("Unexpected provider operation: " + method)


async def close(server):
    (controls.root / "release-stream").touch()
    await asyncio.gather(*helpers, return_exceptions=True)
    if server.process is not None:
        server.process.returncode = 0
    server.initialized = False
    server.session.disconnected()
    server.runtime_activity.disconnected()


original_serve = controls.history.serve


async def serve(app):
    try:
        await original_serve(app)
    finally:
        controls.report["native_retired"] = all(
            s.process.returncode is not None for s in servers
        ) and all(t.done() for t in helpers)
        controls.report["native_history"] = copy.deepcopy(app.conversation)
        (controls.root / "runtime-controls-report.json").write_text(
            json.dumps(controls.report, ensure_ascii=False, indent=2), encoding="utf-8"
        )


controls.history.serve = serve

if __name__ == "__main__":
    from litetui import plugins, rpc
    from litetui.background_input import Bridge
    from litetui.codex_app_server import AppServer
    from litetui.conversation import ConversationRepository

    actual_emit, actual_release, actual_register = (
        rpc.rpc_emit,
        ConversationRepository.release,
        plugins.register_plugins,
    )

    def emit(row):
        if (
            controls.case == "codex-midturn-emission"
            and (row.get("result") or {}).get("kind") == "midturn_queued"
        ):
            controls.report["queue_emission_failed"] = True
            controls.mark("queue-emission-failed")
            raise OSError("Controlled exact confirmation loss")
        return actual_emit(row)

    def release(repository, *args, **kwargs):
        app = controls.history.boundary.app_instance
        if app is not None and repository is app.store and repository.owned:
            bridge = getattr(app, "_native_work_bridge", None)
            if bridge is not None and bridge.closing:
                controls.report["queue_recovered"] = Bridge(app).recovered
        return actual_release(repository, *args, **kwargs)

    def registered(app, registry, order=None, disabled=frozenset()):
        return actual_register(
            app,
            registry,
            order=(
                "litetui.plugins.core_tools",
                "litetui.plugins.convo",
                "litetui.plugins.self_compact",
                "litetui.plugins.misc",
            ),
            disabled=disabled,
        )

    with (
        patch.object(AppServer, "start", start),
        patch.object(AppServer, "request", request),
        patch.object(AppServer, "close", close),
        patch.object(plugins, "register_plugins", registered),
        patch.object(ConversationRepository, "release", release),
        patch.object(rpc, "rpc_emit", emit),
    ):
        controls.history.main()
