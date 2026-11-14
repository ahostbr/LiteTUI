"""Real owned CLI and backend transitions with only external boundaries controlled.

HTTP replies, GPU observation, SDK sessions, inference, registration and unrelated
plugins are synthetic. Settings, stores, authority, factories, lifecycle methods,
connection workers and GUI dispatch remain actual source code.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

source = Path(os.environ["RUST_BACKEND_SWITCH_SOURCE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
sys.path.insert(0, str(source / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests"))
from _isolated_home import isolated_environment

os.environ.update(isolated_environment(root / "isolated-home"))
for key in ("LITETUI_OWNER", "LITETUI_SPAWN_IDENTITY", "LITEHARNESS_SPAWNED_BY",
            "LITEHARNESS_AGENT_ID", "LITEHARNESS_AGENT_NAME", "LITETUI_AGENT_SHELL",
            "LITETUI_BACKEND", "LITETUI_MODEL", "LITETUI_THINKING", "LITETUI_CODEX_ENGINE"):
    os.environ.pop(key, None)
os.environ.update(LITETUI_NO_HARNESS="1", LITETUI_DISABLE_UPDATE_CHECK="1",
                  LITETUI_APPROVAL_HOST="1", LITETUI_HOOKS="off")
case = os.environ.get("RUST_BACKEND_SWITCH_CASE", "success")
target = os.environ.get("RUST_BACKEND_SWITCH_TARGET", "lmstudio")
driver = os.environ.get("RUST_BACKEND_SWITCH_DRIVER", "self")
old_model = "switch-old"
new_model = "switch-new"
global_settings_may_change = False
records: list[dict] = []
calls: list[dict] = []
requests: list[dict] = []
denied: list[str] = []
streams: list = []
app_instance = None
ready = None


def forbidden(*_args, **_kwargs):
    denied.append("Uncontrolled process/network/model-load boundary")
    raise AssertionError(denied[-1])


async def forbidden_async(*args, **kwargs):
    forbidden(*args, **kwargs)


def command(argv, **_kwargs):
    if argv == ["fixture-nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]:
        return NS(stdout="NVIDIA GeForce RTX 4090", returncode=0)
    return forbidden()


def http_json(url, data=None, **_kwargs):
    calls.append({"effect": "http", "url": url, "data": data})
    assert data is None, "Switching must never request model load/unload"
    if (case == "connection-failed" and app_instance is not None
            and app_instance._agent_session.authority.backend == target):
        raise OSError("Controlled connection failed after identity publication")
    if "/api/v0/models" in url:
        return {"data": [{"id": new_model, "type": "llm", "loaded_context_length": 8192}]}
    if url.endswith("/models"):
        return {"data": [{"id": new_model, "status": {"value": "loaded", "args": ["--ctx-size", "8192"]}}]}
    if url.endswith("/props"):
        return {"model_path": "controlled.gguf", "default_generation_settings": {"n_ctx": 8192}}
    return forbidden()


def custom_get(_backend, route):
    assert route == "/models"
    return {"data": [{"id": old_model, "status": {"value": "loaded"}}]}


class ControlledSession:
    """External SDK seam; real ClaudeBackend owns and closes this session."""
    def __init__(self, _options=None, *, outgoing=False):
        self.outgoing = outgoing
        self.lifecycle = NS(cleanup_errors=[])

    async def start(self):
        return {"models": [{"value": old_model, "supportedEffortLevels": ["low", "high"]}]}

    async def close(self):
        if self.outgoing:
            calls.append({"effect": "native-close-start"})
            await asyncio.sleep(0)
            if case == "cleanup-failed":
                self.lifecycle.cleanup_errors.append("controlled SDK cleanup failure")
            calls.append({"effect": "native-close-end"})


class Stream:
    def __init__(self):
        self.closed = False

    def __aiter__(self):
        async def chunks():
            yield NS(choices=[NS(delta=NS(content="After verified backend switch", reasoning_content=None,
                                         tool_calls=None), finish_reason=None)], usage=None)
            yield NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None),
                                finish_reason="stop")], usage=NS(prompt_tokens=11, completion_tokens=5, total_tokens=16))
        return chunks()

    async def close(self):
        self.closed = True


async def inference(transport, *, purpose="turn", **request):
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled summary"))])
    assert app_instance is not None and purpose == "turn"
    assert transport.backend.name == app_instance._agent_session.authority.backend
    assert request["model"] == app_instance._agent_session.authority.model
    requests.append({"backend": transport.backend.name, "model": request["model"],
                     "messages": request["messages"]})
    stream = Stream()
    streams.append(stream)
    return stream


async def settle_workers(app):
    from textual.worker import WorkerCancelled
    workers = list(app.workers)
    results = await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
    for worker, result in zip(workers, results, strict=True):
        if isinstance(result, WorkerCancelled) and worker.group != "chat":
            # Exclusive context refresh retires its superseded observer worker.
            calls.append({"effect": "observer-cancelled", "group": worker.group})
        elif isinstance(result, BaseException):
            raise result


def identity(app):
    authority = app._agent_session.authority
    return {"agent_id": authority.agent_id, "session_id": app.convo_id,
            "backend": authority.backend, "model": authority.model, "thinking": authority.thinking_level,
            "runtime_backend": app.backend.name, "runtime_model": app.model_id,
            "cli_backend": app._cli_initial_backend, "cli_model": app._cli_initial_model,
            "cli_thinking": app._cli_thinking_level}


async def serve(app):
    global ready
    from litetui import gui_rpc, hook_host, rpc, settings_service, store_identity
    assert Path(rpc.__file__).resolve() == (source / "src/litetui/rpc.py").resolve()
    from litetui.conversation import ConversationRepository

    ready = asyncio.Event()
    clients = []
    report = {}
    transitioned = False
    stale_used = False
    actual_write = settings_service._write

    def write(path, raw):
        if case == "persistence-failed" and Path(path) == app.convo_dir / "settings.json":
            raise OSError("Controlled conversation snapshot failure")
        return actual_write(path, raw)

    async def management(cmd):
        nonlocal transitioned, stale_used
        cmd = dict(cmd)
        if cmd["type"] == "gui.backend.set":
            if case in {"stale", "one-shot-stale"} and not (case == "one-shot-stale" and stale_used):
                if driver == "rpc":
                    # Change a snapshot-bound source value, preserving the echoed request token.
                    app.settings.max_tokens_chat += 1
                else:
                    cmd["snapshot"] = "0" * 64
                stale_used = True
            if case == "foreign":
                cmd["agent_id"] = "b769dc1f-7bbe-4b7a-a3da-339e9d309798"
            if case == "missing-model":
                cmd["model"] = "absent-selected-model"
            if case == "held":
                app._pending_input.append({"content": "UNSENT HELD INPUT", "source": "queued", "tool_profile": "strict"})
            with patch.object(settings_service, "_write", write):
                value = await gui_rpc.async_dispatch(app, cmd)
            transitioned = value.get("status") in {"connected", "switched"}
            return value
        value = await gui_rpc.async_dispatch(app, cmd)
        if transitioned and case == "policy-drift" and cmd["type"] == "gui.state":
            value["tool_policy"]["tools_enabled"] = True
        if transitioned and case == "history-drift" and cmd["type"] in {"gui.conversations.read", "gui.history.native_snapshot"}:
            if "context_sha256" in value:
                value["context_sha256"] = "0" * 64
            else:
                value["messages"].append({"role": "assistant", "content": "Unexpected changed history"})
        return value

    with ExitStack() as stack:
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, forbidden))
        try:
            async with asyncio.timeout(35), app.run_test(size=(100, 32)):
                await asyncio.wait_for(ready.wait(), 12)
                await hook_host.drain_lifecycle(app)
                assert next(row for row in records if row.get("type") == "ready")["launch_status"] == "ready"
                await asyncio.wait_for(settle_workers(app), 5)
                clients.append(app.client)
                if case != "empty":
                    app.conversation = [{"role": "system", "content": "Switch fixture system"},
                                        {"role": "user", "content": "History before backend switch"},
                                        {"role": "assistant", "content": "X" * (2200 * 1024) if case == "oversized-history" else "Retained answer before switch"}]
                    app._prompt_store_identity = store_identity.capture(app)
                    app._store_injected = True
                    app._materialise_convo()
                    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                    await hook_host.drain_lifecycle(app)
                    await asyncio.wait_for(settle_workers(app), 5)
                before = identity(app)
                home_before = (app._agent_session.memory_root / "settings.json").read_bytes()
                transcript_before = app.convo_path.read_bytes() if app.convo_path.exists() else b""
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "switch-hello", "protocol_version": 1})
                catalog = await gui_rpc.async_dispatch(app, {"type": "gui.backends.list", "id": "switch-catalog",
                                                            "session_id": app.convo_id})
                if app.backend.name == "claude":
                    app.backend.session = ControlledSession(outgoing=True)
                    app.backend.segment_id = "fixture-outgoing-segment"
                if driver == "rpc":
                    (root / "switch-identity.json").write_text(json.dumps({**before, "catalog": catalog}), encoding="utf-8")
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        cmd = json.loads(line)
                        with (root / "switch-commands.jsonl").open("a", encoding="utf-8") as log:
                            log.write(json.dumps(cmd) + "\n")
                        if cmd["type"] == "shutdown":
                            rpc._dispatch(app, cmd)
                            break
                        if cmd["type"].startswith("gui."):
                            try:
                                value = await management(cmd)
                                rpc.publish_management_result(app, cmd, value)
                            except Exception as exc:  # noqa: BLE001 - mirror the actual RPC error boundary
                                rpc._respond(cmd["id"], ok=False, error=str(exc))
                        else:
                            rpc._dispatch(app, cmd)
                            await hook_host.drain_lifecycle(app)
                            await asyncio.wait_for(settle_workers(app), 5)
                    report = {"before": before, "after": identity(app)}
                else:
                    cmd = {"type": "gui.backend.set", "id": "switch-operation", "version": 1,
                           "session_id": app.convo_id, "agent_id": before["agent_id"],
                           "snapshot": catalog["snapshot"], "name": target, "model": None}
                    result = await management(cmd)
                    after = identity(app)
                    report = {"before": before, "after": after, "catalog": catalog, "result": result,
                              "home_unchanged": (app._agent_session.memory_root / "settings.json").read_bytes() == home_before,
                              "transcript_unchanged": app.convo_path.read_bytes() == transcript_before,
                              "old_client_closed": clients[0].is_closed(),
                              "held": list(app._pending_input),
                              "saved_agent": json.loads((app._agent_session.memory_root / "settings.json").read_text()),
                              "failure_latch": getattr(app, "_backend_transition_error", None)}
                    if result["status"] == "switched":
                        report["state"] = await gui_rpc.async_dispatch(app, {"type": "gui.state", "id": "after-state", "include_conversations": False})
                        report["history"] = await gui_rpc.async_dispatch(app, {"type": "gui.conversations.read", "id": "after-history", "session_id": app.convo_id})
                        report["after_catalog"] = await gui_rpc.async_dispatch(app, {"type": "gui.backends.list", "id": "after-catalog", "session_id": app.convo_id})
                        # /backend leaves the model unchosen; /model picks one (Ryan 2026-11-12).
                        if result["connected"]:
                            report["model_pick"] = await gui_rpc.async_dispatch(app, {"type": "gui.models.select", "id": "after-pick", "slug": new_model})
                            report["after_pick"] = identity(app)
                        rpc._dispatch(app, {"type": "prompt", "id": "after-switch-prompt", "message": "Explicit next prompt", "tool_profile": "strict"})
                        await hook_host.drain_lifecycle(app)
                        await asyncio.wait_for(settle_workers(app), 8)
                    elif result.get("close_required"):
                        # The source must block late commands even if the host has not closed yet.
                        rpc._dispatch(app, {"type": "prompt", "id": "must-refuse", "message": "Must not dispatch", "tool_profile": "strict"})
                        await hook_host.drain_lifecycle(app)
                        await asyncio.wait_for(settle_workers(app), 5)
                    report["messages"] = ConversationRepository.read(app.convo_path)[1]
                clients.append(app.client)
        finally:
            for client in clients:
                await client.close()
            await app.client.close()
    from litetui import app as app_module
    assert Path(app_module.__file__).resolve() == source.resolve() / "src/litetui/app.py"
    report.update(calls=calls, requests=requests, denied=denied, source_app=app_module.__file__,
                  launch_arguments=sys.argv[1:])
    (root / "switch-report.json").write_text(json.dumps(report), encoding="utf-8")


def run(app, **kwargs):
    assert kwargs == {"headless": True}
    asyncio.run(serve(app))


async def launch_codex_start(server):
    server.process = NS(returncode=None)
    server.native_bridge = NS(policy=None)
    server.initialized = True


async def launch_codex_request(server, method, params):
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        return {"data": [{"model": old_model, "displayName": "Controlled Codex",
                          "defaultReasoningEffort": "low",
                          "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]}
    raise AssertionError(f"Unexpected startup provider request: {method}")


async def launch_codex_close(server):
    if server.process is not None:
        server.process.returncode = 0
    server.initialized = False
    server.runtime_activity.disconnected()


def main():
    import httpx

    from litetui import (
        backend_catalog,
        claude_backend,
        gpu_gate,
        harness,
        image_viewer,
        llm_backend,
        mcp_client,
        model_transport,
        plugins,
        rpc,
        thinking_probe,
        ttyguard,
        update_check,
    )
    from litetui.app import LiteTUI
    from litetui.custom_backend import CustomBackend

    original_init, original_emit = LiteTUI.__init__, rpc.rpc_emit
    global_before = (root / "settings.json").read_bytes()
    def constructed(app, *args, **kwargs):
        global app_instance
        app_instance = app
        original_init(app, *args, **kwargs)
    def emit(row):
        records.append(row)
        original_emit(row)
        if row.get("type") == "ready" and ready is not None:
            ready.set()
    def registered(seat):
        seat.registered, seat.error = True, ""
        return True
    with ExitStack() as stack:
        if case == "launch-defaults":
            from litetui.codex_app_server import AppServer
            stack.enter_context(patch.object(AppServer, "start", launch_codex_start))
            stack.enter_context(patch.object(AppServer, "request", launch_codex_request))
            stack.enter_context(patch.object(AppServer, "close", launch_codex_close))
        for obj, name, replacement in (
            (platform, "node", lambda: "controlled-host"), (subprocess, "Popen", forbidden),
            (httpx.Client, "send", forbidden), (httpx.AsyncClient, "send", forbidden_async),
            (ttyguard, "run", command), (ttyguard, "popen", forbidden),
            (gpu_gate, "nvidia_smi", lambda: "fixture-nvidia-smi"),
            # Display-only readiness marks probe engines exactly as Python's /backend does
            # (plugins/model_switch.backend_rows); this fixture forbids those boundaries.
            (backend_catalog, "_marks", lambda _app, _names: {}),
            (plugins, "PLUGIN_LOAD_ORDER", ("litetui.plugins.core_tools", "litetui.plugins.self_compact")),
            (rpc, "start_rpc_reader", lambda _app: None), (rpc, "rpc_emit", emit),
            (harness.Seat, "register", registered), (update_check, "start_background_check", lambda _app: None),
            (image_viewer, "init_image_backend", lambda: None),
            (LiteTUI, "__init__", constructed), (LiteTUI, "run", run),
            (model_transport.OpenAITransport, "create", inference),
            (CustomBackend, "_get", custom_get), (llm_backend, "_http_json", http_json),
            (llm_backend, "_healthy", lambda *_: case != "llama-dead"),
            (llm_backend, "llama_executable", lambda _settings: Path("controlled-llama-server")),
            (llm_backend.router_record, "read", lambda: None),
            (llm_backend.LlamaCppBackend, "_spawn", forbidden),
            (claude_backend, "ClaudeSession", ControlledSession),
            (claude_backend, "sdk_module", lambda: NS(ClaudeAgentOptions=lambda **kwargs: kwargs)),
            (thinking_probe, "get_effective_levels", lambda *_args, **_kwargs: None),
        ):
            stack.enter_context(patch.object(obj, name, replacement))
        for cls in (llm_backend.LMStudioBackend, llm_backend.LlamaCppBackend):
            for method in ("load", "apply_load_settings", "unload"):
                stack.enter_context(patch.object(cls, method, forbidden_async))
        for method in ("load", "connect", "reload_configs"):
            stack.enter_context(patch.object(mcp_client.MCPManager, method, forbidden))
        gpu_gate.is_rtx_5090.cache_clear()
        try:
            spec = importlib.util.spec_from_file_location("litetui._actual_switch_cli", source / "src/litetui/cli.py")
            assert spec and spec.loader
            cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli)
            cli.main()
        finally:
            gpu_gate.is_rtx_5090.cache_clear()
        assert not denied, denied
        assert all(stream.closed for stream in streams)
        # A device-wide engine preference save (develop 5f5ef63f) is the one sanctioned write.
        if not global_settings_may_change:
            assert (root / "settings.json").read_bytes() == global_before


if __name__ == "__main__":
    main()
