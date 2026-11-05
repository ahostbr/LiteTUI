"""Owned MCP cleanup and truthful retained failure, without external transports."""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from litetui import conversation_action, gui_rpc, mcp_client, native_mcp_shutdown
from litetui.plugins import mcp_manage

pytestmark = pytest.mark.real_mcp_load


class Server:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0
        self.tools = [{"name": "private"}]
        self.error = None
        self.stopped = False

    def stop(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("SECRET fixture failure")
        self.stopped = True


def owner(tmp_path, failures=0):
    manager = mcp_client.MCPManager(tmp_path)
    server = Server(failures)
    manager.configs["private"] = {"command": "never-spawn"}
    manager.servers["private"] = server
    app = NS(mcp=manager, _owned_mcp_manager=manager)
    return app, manager, server


def test_disconnect_bool_is_not_a_stop_proof_and_independent_readback_is_failed(
    tmp_path,
):
    _app, manager, server = owner(tmp_path, 1)
    was = manager.disconnect("private")
    assert was is True and manager.servers["private"] is server
    message = mcp_manage._disconnect_message(manager, "private", was)
    assert "unconfirmed" in message and "Disconnected" not in message
    assert "SECRET" not in message
    assert manager.describe()[0]["state"] == "failed"
    assert manager.inspect_cached()["servers"][0]["state"] == "failed"
    assert "SECRET" not in str(manager.describe())
    assert manager.disconnect("private") is True
    assert server.stopped and not manager._stop_failed and not manager.servers
    assert manager.describe()[0]["state"] == "stopped"
    assert "Disconnected" in mcp_manage._disconnect_message(manager, "private", True)


@pytest.mark.asyncio
async def test_legacy_rpc_reports_retention_then_independently_reads_exact_retry(
    tmp_path,
):
    app, manager, server = owner(tmp_path, 1)
    rebuilt = []
    app.rebuild_mcp_dispatch = lambda: rebuilt.append(manager.dispatch())
    command = {"type": "gui.mcp.disconnect", "name": "private"}
    with pytest.raises(ValueError, match="unconfirmed"):
        await gui_rpc.async_dispatch(app, command)
    assert manager.servers["private"] is server and rebuilt
    rows = await gui_rpc.async_dispatch(app, {"type": "gui.mcp.list"})
    assert rows[0]["state"] == "failed"
    rows = await gui_rpc.async_dispatch(app, command)
    assert rows[0]["state"] == "stopped" and server.stopped


@pytest.mark.asyncio
async def test_shutdown_retains_failed_stop_and_explicit_retry_only_joins_owned_manager(
    tmp_path,
):
    app, manager, server = owner(tmp_path, 1)
    with pytest.raises(RuntimeError, match="retains unconfirmed"):
        await native_mcp_shutdown.settle_before_shutdown(app)
    assert manager._closing and manager.servers["private"] is server
    assert manager.connect("private").startswith("closing")
    await native_mcp_shutdown.settle_before_shutdown(app)
    assert server.calls == 2 and server.stopped
    assert not manager.servers and not manager._stop_failed


@pytest.mark.asyncio
async def test_shutdown_never_retargets_a_replaced_manager(tmp_path):
    app, original, server = owner(tmp_path)
    _other, replacement, foreign = owner(tmp_path / "other")
    app.mcp = replacement
    with pytest.raises(RuntimeError, match="manager changed"):
        await native_mcp_shutdown.settle_before_shutdown(app)
    assert server.stopped and not original.servers
    assert not foreign.stopped and replacement.servers["private"] is foreign
    app.mcp = original
    await native_mcp_shutdown.settle_before_shutdown(app)
    assert not foreign.stopped


@pytest.mark.asyncio
async def test_cancelled_shutdown_waits_for_the_captured_stop_thread(tmp_path):
    app, manager, server = owner(tmp_path)
    entered, release, retired = threading.Event(), threading.Event(), threading.Event()

    def stop():
        entered.set()
        assert release.wait(5)
        server.stopped = True
        retired.set()

    server.stop = stop
    task = asyncio.create_task(native_mcp_shutdown.settle_before_shutdown(app))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and manager._closing and not retired.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retired.is_set() and not manager.servers


@pytest.mark.asyncio
async def test_native_quit_denies_ack_until_failed_owned_cleanup_is_retried(
    tmp_path, monkeypatch
):
    from litetui import (
        background_input,
        conversation_shutdown,
        native_engine_shutdown,
        native_glassbox,
        native_runtime_controls,
        native_work_execution,
    )

    app, manager, server = owner(tmp_path, 1)
    replies, exits = [], []
    app.exit = lambda: exits.append("exit")

    async def empty(_app):
        return {}

    async def settled(_app):
        return None

    monkeypatch.setattr(conversation_action, "settle_all_before_shutdown", empty)
    monkeypatch.setattr(native_runtime_controls, "shutdown", empty)
    for module in (
        background_input,
        native_work_execution,
        conversation_shutdown,
        native_engine_shutdown,
        native_glassbox,
    ):
        monkeypatch.setattr(module, "settle_before_shutdown", settled)
    app._conversation_rewrite_shutdown_callbacks = [
        lambda **reply: replies.append(reply)
    ]
    await conversation_action._finish_exit(app)
    assert replies[-1]["ok"] is False and not exits
    assert manager.servers["private"] is server and not server.stopped
    app._conversation_rewrite_shutdown_callbacks = [
        lambda **reply: replies.append(reply)
    ]
    await conversation_action._finish_exit(app)
    assert replies[-1]["ok"] is True and exits == ["exit"] and server.stopped


@pytest.mark.asyncio
async def test_app_teardown_retains_lease_on_failure_and_joins_before_release(
    tmp_path, monkeypatch
):
    from litetui import (
        conversation_rewrite,
        conversation_shutdown,
        launch_options,
        native_engine_shutdown,
        native_glassbox,
        resource_session_lifecycle,
    )

    # Execute the current source method unchanged, replacing only Textual's base teardown.
    path = Path(__file__).resolve().parents[1] / "src/litetui/app.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "LiteTUI"
    )
    method = next(
        n
        for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_shutdown"
    )
    base = ast.parse(
        "class Base:\n    async def _shutdown(self): self.events.append('base')\n"
    ).body[0]
    subject = ast.ClassDef(
        name="Subject",
        bases=[ast.Name(id="Base", ctx=ast.Load())],
        keywords=[],
        body=[method],
        decorator_list=[],
    )
    scope = {"asyncio": asyncio, "conversation_rewrite": conversation_rewrite}
    exec(  # noqa: S102
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[base, subject], type_ignores=[])
            ),
            str(path),
            "exec",
        ),
        scope,
    )
    _owner, manager, server = owner(tmp_path, 1)
    app = scope["Subject"]()
    app.mcp = app._owned_mcp_manager = manager
    app.events = []

    async def noop(*_args):
        pass

    async def release():
        assert server.stopped and not manager.servers
        app.events.append("released")

    app._release_owned_storage = release
    app._settle_before_teardown = noop
    for module in (
        conversation_rewrite,
        conversation_shutdown,
        native_engine_shutdown,
        native_glassbox,
    ):
        monkeypatch.setattr(module, "settle_before_shutdown", noop)
    monkeypatch.setattr(resource_session_lifecycle, "begin_shutdown", lambda _app: {})
    monkeypatch.setattr(launch_options, "stop_custom", lambda _app: None)
    with pytest.raises(RuntimeError, match="retains unconfirmed"):
        await app._shutdown()
    assert not app.events and manager.servers["private"] is server
    await app._shutdown()
    assert app.events == ["base", "released"]


@pytest.mark.asyncio
async def test_stop_all_serializes_with_a_concurrent_explicit_disconnect(tmp_path):
    app, manager, server = owner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def stop():
        calls.append("stop")
        entered.set()
        assert release.wait(5)
        server.stopped = True

    server.stop = stop
    shutdown = asyncio.create_task(native_mcp_shutdown.settle_before_shutdown(app))
    assert await asyncio.to_thread(entered.wait, 3)
    retry = asyncio.create_task(asyncio.to_thread(manager.disconnect, "private"))
    for _ in range(100):
        if manager._maint_active:
            break
        await asyncio.sleep(0.005)
    assert manager._maint_active and not retry.done() and calls == ["stop"]
    release.set()
    await shutdown
    assert await retry is False
    assert calls == ["stop"] and not manager.servers


@pytest.mark.asyncio
async def test_legacy_rpc_never_reports_a_replacement_managers_state_as_its_result(
    tmp_path,
):
    app, manager, server = owner(tmp_path)
    _other, replacement, foreign = owner(tmp_path / "other")
    rebuilt = []
    app.rebuild_mcp_dispatch = lambda: rebuilt.append(True)

    def stop():
        app.mcp = replacement
        server.stopped = True

    server.stop = stop
    with pytest.raises(ValueError, match="manager changed"):
        await gui_rpc.async_dispatch(
            app, {"type": "gui.mcp.disconnect", "name": "private"}
        )
    assert server.stopped and not manager.servers and not rebuilt
    assert not foreign.stopped and replacement.servers["private"] is foreign


@pytest.mark.asyncio
async def test_cancelled_legacy_rpc_joins_its_captured_operation_before_returning(
    tmp_path,
):
    app, manager, server = owner(tmp_path)
    entered, release = threading.Event(), threading.Event()
    rebuilt = []
    app.rebuild_mcp_dispatch = lambda: rebuilt.append(True)

    def stop():
        entered.set()
        assert release.wait(5)
        server.stopped = True

    server.stop = stop
    task = asyncio.create_task(
        gui_rpc.async_dispatch(app, {"type": "gui.mcp.disconnect", "name": "private"})
    )
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not server.stopped
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.stopped and not manager.servers and rebuilt == [True]
