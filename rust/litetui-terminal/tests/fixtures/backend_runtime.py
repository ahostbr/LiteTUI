"""Actual CLI/owned startup/generic stream, with controlled external boundaries.

Headless Textual runs in memory. Backend catalog/readiness/load/start observations,
GPU command output, inference transport, registration and unrelated plugins are
fixtures. This is contract evidence, not hardware, engine or provider acceptance.
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
import traceback
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

source = Path(os.environ["RUST_BACKEND_SOURCE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
sys.path.insert(0, str(source / "src"))
sys.path.insert(0, str(source / "tests"))
from _isolated_home import isolated_environment

home = root / "Users" / "fixture"
os.environ.update(isolated_environment(home))
for key in ("LITETUI_OWNER", "LITETUI_SPAWN_IDENTITY", "LITEHARNESS_SPAWNED_BY",
            "LITEHARNESS_AGENT_ID", "LITEHARNESS_AGENT_NAME", "LITETUI_AGENT_SHELL",
            "LITETUI_BACKEND", "LITETUI_MODEL", "LITETUI_THINKING"):
    os.environ.pop(key, None)
os.environ.update(LITETUI_NO_HARNESS="1", LITETUI_DISABLE_UPDATE_CHECK="1",
                  LITETUI_APPROVAL_HOST="1", LITETUI_HOOKS="off")
driver = os.environ.get("RUST_BACKEND_DRIVER", "self")
mode = os.environ.get("RUST_BACKEND_CASE", "success")
gpu = os.environ.get("RUST_BACKEND_GPU", "5090")
selected = sys.argv[sys.argv.index("--backend") + 1]
model = sys.argv[sys.argv.index("--model") + 1]
records: list[dict] = []
calls: list[dict] = []
requests: list[dict] = []
denied: list[str] = []
streams: list = []
ready = None
app_instance = None
authority_snapshot = None
custom_process = None
explicit_start = "--start-server" in sys.argv or any(sys.argv[i:i+2] == ["--server-mode", "start"] for i in range(len(sys.argv)))


def forbidden(*_args, **_kwargs):
    denied.append(" > ".join(f"{frame.name}@{Path(frame.filename).name}:{frame.lineno}" for frame in traceback.extract_stack(limit=6)[:-1]))
    raise AssertionError("Uncontrolled process/network/hardware effect")


async def forbidden_async(*args, **kwargs):
    forbidden(*args, **kwargs)


def command(argv, **_kwargs):
    if argv == ["fixture-nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]:
        calls.append({"effect": "gpu-observation", "case": gpu})
        if gpu == "error":
            raise OSError("synthetic GPU observation failed")
        return NS(stdout="NVIDIA GeForce RTX " + gpu if gpu else "", returncode=0)
    if len(argv) >= 3 and argv[:3] == ["fixture-lms", "server", "start"]:
        calls.append({"effect": "server-start", "argv": argv})
        return NS(returncode=0)
    return forbidden()


def spawn(argv, **kwargs):
    global custom_process
    assert selected == "custom" and explicit_start and custom_process is None
    assert argv == ["fixture-server", "--model", model, "--port", "45123"]
    assert kwargs.get("kill_on_close") is True and "shell" not in kwargs
    calls.append({"effect": "server-start", "argv": argv})
    process = NS(pid=-123, returncode=None)
    process.poll = lambda: process.returncode
    process.wait = lambda **_kwargs: process.returncode
    custom_process = process
    return process


def stop(pid, process):
    assert custom_process is not None
    assert process is custom_process and pid == -123
    calls.append({"effect": "server-stop"})
    process.returncode = 0
    return True


class Stream:
    def __init__(self):
        self.closed = False

    def __aiter__(self):
        async def chunks():
            yield NS(choices=[NS(delta=NS(content="Controlled backend answer",
                                         reasoning_content="Controlled reasoning", tool_calls=None),
                                finish_reason=None)], usage=None)
            yield NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None),
                                finish_reason="stop")],
                     usage=NS(prompt_tokens=12, completion_tokens=7, total_tokens=19))
        return chunks()

    async def close(self):
        self.closed = True


async def inference(transport, *, purpose="turn", retry_notice=None, **request):
    assert transport.backend.name == selected and request["model"] == model
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled summary"))])
    assert purpose == "turn"
    requests.append({"backend": transport.backend.name, "model": request["model"],
                     "max_tokens": request.get("max_tokens")})
    stream = Stream()
    streams.append(stream)
    return stream


async def serve(app):
    global ready, authority_snapshot
    from litetui import gui_rpc, hook_host, rpc

    ready = asyncio.Event()
    authority_snapshot = {"backend": app._agent_session.authority.backend,
                          "model": app._agent_session.authority.model}
    agent_settings = app._agent_session.memory_root / "settings.json"
    agent_before = agent_settings.read_bytes()
    # Loop construction already made its Windows self-pipe; no external socket
    # operations may occur from startup through source teardown.
    with ExitStack() as stack:
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, forbidden))
        try:
            async with asyncio.timeout(25), app.run_test(size=(100, 32)) as pilot:
                await asyncio.wait_for(ready.wait(), 12)
                await hook_host.drain_lifecycle(app)
                status = next(row for row in records if row.get("type") == "ready")
                if driver == "rpc":
                    while True:
                        line = await asyncio.to_thread(sys.stdin.readline)
                        if not line:
                            break
                        request = json.loads(line)
                        with (root / "backend-commands.jsonl").open("a", encoding="utf-8") as log:
                            log.write(json.dumps(request) + "\n")
                        if request["type"].startswith("gui."):
                            try:
                                result = await gui_rpc.async_dispatch(app, request)
                                rpc._respond(request.get("id"), ok=True, result=result)
                            except Exception as exc:  # noqa: BLE001 - same isolation as the source RPC management boundary
                                rpc._respond(request.get("id"), ok=False, error=str(exc))
                        else:
                            rpc._dispatch(app, request)
                        if request["type"] == "shutdown":
                            break
                        if request["type"] == "prompt":
                            await hook_host.drain_lifecycle(app)
                            await asyncio.wait_for(app.workers.wait_for_complete(), 5)
                elif status["launch_status"] == "ready":
                    await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "fixture-hello", "protocol_version": 1})
                    accepted = hook_host.accept_prompt_origin(app, {
                        "content": "Controlled prompt", "source": "rpc", "operation_id": "backend-operation"})
                    assert accepted is not None
                    await app._stream_inline(origin=accepted)
                    await pilot.pause()
                await asyncio.wait_for(app.workers.wait_for_complete(), 5)
        finally:
            # run_test does not own the CLI client; retire fixture resources.
            await app.client.close()
            app.backend.shutdown()
        assert app.client.is_closed()
    assert agent_settings.read_bytes() == agent_before, "Invocation flags persisted into agent settings"


def run(app, **kwargs):
    assert kwargs == {"headless": True}
    asyncio.run(serve(app))


def main():
    import httpx

    from litetui import (
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
    from litetui.cline_backend import ClineBackend
    from litetui.custom_backend import CustomBackend
    from litetui.free_tier import FreeBackend
    from litetui.ninfer_backend import NInferBackend
    from litetui.strata_backend import StrataBackend

    original_emit = rpc.rpc_emit
    original_init = LiteTUI.__init__
    global_before = (root / "settings.json").read_bytes()

    def emit(row):
        records.append(row)
        original_emit(row)
        if row.get("type") == "ready" and ready is not None:
            ready.set()

    def constructed(app, *args, **kwargs):
        global app_instance
        app_instance = app
        original_init(app, *args, **kwargs)

    def registered(seat):
        calls.append({"effect": "registration", "backend": seat.backend, "model": seat.model})
        seat.registered, seat.error = True, ""
        return True

    async def ensure(backend):
        calls.append({"effect": "ensure", "backend": backend.name})
        if mode == "dead":
            raise llm_backend.BackendError("Controlled endpoint unavailable; no fallback")
        return "ok"

    async def listing(backend):
        if selected == "custom" and explicit_start and custom_process is None:
            raise llm_backend.BackendError("Controlled custom server has not started")
        key = "another-model" if mode == "unavailable" else model
        return [llm_backend.ModelRow(key=key, path=None, source="fixture", loaded=True)]

    async def info(backend, key):
        return 8192, "llm", True

    async def chat_ready(backend, key):
        assert key == model
        calls.append({"effect": "chat-ready", "backend": backend.name, "model": key})

    async def load(backend, key, *, ctx=None, **_kwargs):
        calls.append({"effect": "load", "backend": backend.name, "model": key, "context": ctx})

    async def start(backend):
        calls.append({"effect": "engine-start", "backend": backend.name})
        return "controlled engine started"

    with ExitStack() as stack:
        for obj, name, replacement in (
            (platform, "node", lambda: "controlled-host"),
            (subprocess, "Popen", forbidden), (httpx.Client, "send", forbidden),
            (httpx.AsyncClient, "send", forbidden_async),
            (ttyguard, "run", command), (ttyguard, "popen", spawn), (ttyguard, "kill_tree", stop),
            (gpu_gate, "nvidia_smi", lambda: "fixture-nvidia-smi" if gpu != "absent" else None),
            (plugins, "PLUGIN_LOAD_ORDER", ("litetui.plugins.core_tools", "litetui.plugins.self_compact")),
            (rpc, "start_rpc_reader", lambda _app: None), (rpc, "rpc_emit", emit),
            (harness.Seat, "register", registered),
            (update_check, "start_background_check", lambda _app: None),
            (image_viewer, "init_image_backend", lambda: None),
            (LiteTUI, "__init__", constructed), (LiteTUI, "run", run),
            (model_transport.OpenAITransport, "create", inference),
            (llm_backend, "_healthy", lambda *_args: mode != "dead"),
            (ClineBackend, "api_key", lambda _self: "fixture-token"),
            (thinking_probe, "get_effective_levels", lambda *_args, **_kwargs: None),
        ):
            stack.enter_context(patch.object(obj, name, replacement))
        for method in ("load", "connect", "reload_configs"):
            stack.enter_context(patch.object(mcp_client.MCPManager, method, forbidden))
        for cls in (CustomBackend, llm_backend.LMStudioBackend, llm_backend.LlamaCppBackend,
                    NInferBackend, StrataBackend, ClineBackend, FreeBackend):
            for name, replacement in (("ensure_running", ensure), ("list_models", listing),
                                      ("model_info", info), ("ensure_chat_ready", chat_ready),
                                      ("load", load)):
                stack.enter_context(patch.object(cls, name, replacement))
            if hasattr(cls, "loaded_models"):
                stack.enter_context(patch.object(cls, "loaded_models", lambda _self: [model]))
        for cls in (NInferBackend, StrataBackend):
            stack.enter_context(patch.object(cls, "start_engine", start))
        import shutil
        which = shutil.which
        stack.enter_context(patch.object(shutil, "which", lambda name: "fixture-lms" if name == "lms" else which(name)))
        gpu_gate.is_rtx_5090.cache_clear()
        try:
            # A native test may use a litetui.cli overlay; import actual source
            # by file so this fixture cannot recursively reenter that overlay.
            spec = importlib.util.spec_from_file_location("litetui._backend_actual_cli", source / "src/litetui/cli.py")
            assert spec is not None and spec.loader is not None
            cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli)
            cli.main()
        finally:
            gpu_gate.is_rtx_5090.cache_clear()
        assert not denied, denied
        assert (root / "settings.json").read_bytes() == global_before
        assert all(stream.closed for stream in streams)
        assert app_instance is not None
        report = {"type": "fixture_report", "calls": calls, "requests": requests, "denied": denied,
                  "backend": app_instance.backend.name, "authority": authority_snapshot,
                  "launch": vars(app_instance._launch_options),
                  "source_backends": list(llm_backend.BACKEND_NAMES)}
        if driver == "rpc":
            (root / "backend-report.json").write_text(json.dumps(report), encoding="utf-8")
        else:
            original_emit(report)


if __name__ == "__main__":
    main()
