"""Controlled shutdown joins; no provider, process or desktop interaction."""
from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import conversation_shutdown as shutdown
from litetui import native_engine_shutdown

OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
IDENTITY = {"action_id": "33333333-3333-4333-8333-333333333333", "action": "new",
            "from_session_id": OLD, "target_session_id": NEW,
            "agent_id": "44444444-4444-4444-8444-444444444444", "backend": "custom", "model": "model"}


def app():
    events = []
    return SimpleNamespace(_conversation_transition_protocol=True, _conversation_transition_pending=False,
                           _conversation_transition_shutdown_identity=copy.deepcopy(IDENTITY),
                           convo_id=OLD, _hook_lifecycle=[], _hook_lock=asyncio.Lock(),
                           _claude_closing=None, events=events, exit=lambda: events.append("exit"))


def callback(host):
    return lambda **fields: host.events.append(fields)


@pytest.mark.asyncio
async def test_legacy_shutdown_is_untouched():
    host = SimpleNamespace()
    assert not shutdown.request_shutdown(host)
    assert await shutdown.settle_before_shutdown(host) is None
    assert not vars(host)


@pytest.mark.asyncio
async def test_empty_review_protocol_reports_no_action_and_caches_independent_proof():
    host = app()
    host._conversation_transition_shutdown_identity = None
    assert shutdown.request_shutdown(host, callback(host))
    assert host._gui_quitting and host._conversation_transition_shutdown_requested
    assert shutdown.request_shutdown(host, callback(host))
    await host._conversation_shutdown_exit_task
    assert host.events[-1] == "exit" and len(host.events) == 3
    first, second = host.events[:2]
    assert first == second and first["ok"] is True
    report = first["result"]["conversation_shutdown"]
    assert all(report[key] is None for key in shutdown.IDENTITY)
    assert report["pending_at_close"] is False and report["effects_verified"] is False
    report["worker_settled"] = False
    assert (await shutdown.settle_before_shutdown(host))["worker_settled"] is True


@pytest.mark.asyncio
async def test_active_action_joins_through_repeated_waiter_cancellation_before_exit():
    host = app()
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def action():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            host.events.append("worker retired")
            host.convo_id = NEW
            host._conversation_transition_pending = False
    task = asyncio.create_task(action())
    host._conversation_transition_task = task
    host._conversation_transition_pending = True
    await entered.wait()
    waiter = asyncio.create_task(shutdown.settle_before_shutdown(host))
    await cancelled.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    assert not waiter.done() and not host.events
    assert shutdown.request_shutdown(host, callback(host))
    assert shutdown.request_shutdown(host, callback(host))
    release.set()
    report = await waiter
    await host._conversation_shutdown_exit_task
    assert report["pending_at_close"] is True and report["current_session_id"] == NEW
    assert all(report[key] == value for key, value in IDENTITY.items())
    assert host.events[0] == "worker retired" and host.events[-1] == "exit"
    assert all(item["ok"] for item in host.events[1:-1])
    assert task.done() and not task.cancelled()


@pytest.mark.asyncio
async def test_completed_caller_is_not_cancelled_and_later_normal_hooks_are_not_transition_work():
    host = app()
    caller = asyncio.current_task()
    assert caller is not None
    host._conversation_transition_task = caller
    await host._hook_lock.acquire()
    host._hook_lifecycle.append("ordinary later hook")
    try:
        report = await shutdown.settle_before_shutdown(host)
        assert report["pending_at_close"] is False
        assert not caller.cancelling()
    finally:
        host._hook_lock.release()


@pytest.mark.asyncio
async def test_active_action_cannot_join_itself():
    host = app()
    host._conversation_transition_pending = True
    host._conversation_transition_task = asyncio.current_task()
    with pytest.raises(RuntimeError, match="cannot join itself"):
        await shutdown.settle_before_shutdown(host)
    assert not hasattr(host, "_conversation_shutdown_settlement_task")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_task", "still_pending", "hooks", "provider", "cleanup", "identity"])
async def test_unproven_settlement_never_acknowledges_or_exits(fault):
    host = app()
    host._conversation_transition_pending = True
    if fault == "cleanup":
        host._conversation_transition_cleanup_error = "unproven retained process"
    if fault == "identity":
        host._conversation_transition_shutdown_identity = {"action_id": ""}
    if fault == "hooks":
        host._hook_lifecycle.append("unsettled")
    retained = None
    if fault == "provider":
        retained = asyncio.create_task(asyncio.Event().wait())
        host._claude_closing = [retained]
    entered = asyncio.Event()
    async def action():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if fault != "still_pending":
                host._conversation_transition_pending = False
    task = None
    if fault != "missing_task":
        task = asyncio.create_task(action())
        host._conversation_transition_task = task
        await entered.wait()
    try:
        assert shutdown.request_shutdown(host, callback(host))
        await host._conversation_shutdown_exit_task
        assert len(host.events) == 1 and host.events[0]["ok"] is False
        assert host.events[0]["result"] is None and "unproven" in host.events[0]["error"]
        assert "exit" not in host.events
    finally:
        if retained:
            retained.cancel()
            await asyncio.gather(retained, return_exceptions=True)
        if task:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("clean", [True, False])
async def test_engine_protocol_proof_is_preserved_when_conversation_inspection_also_negotiated(monkeypatch, clean):
    host = app()
    proof = {"worker_settled": True, "shutdown": {"owned": True, "main_exited": clean, "retained": not clean}}
    async def engine(_host):
        return proof
    monkeypatch.setattr(native_engine_shutdown, "settle_before_shutdown", engine)
    assert shutdown.request_shutdown(host, callback(host))
    await host._conversation_shutdown_exit_task
    response = host.events[0]
    assert response["ok"] is clean
    if clean:
        assert response["result"]["engine_shutdown"] == proof
        assert "conversation_shutdown" in response["result"]
        assert host.events[-1] == "exit"
    else:
        assert response["result"] is None and host.events == [response]


def shutdown_method():
    path = Path(__file__).resolve().parents[1] / "src/litetui/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LiteTUI")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_shutdown")
    scope = {"asyncio": asyncio}
    base = ast.parse("class Base:\n    async def _shutdown(self): self.events.append('base')\n").body[0]
    subject = ast.ClassDef(name="Subject", bases=[ast.Name(id="Base", ctx=ast.Load())], keywords=[], body=[method], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[base, subject], type_ignores=[]))
    # Execute only the saved repository method, avoiding app construction and device startup.
    exec(compile(module, str(path), "exec"), scope)  # noqa: S102
    return scope["Subject"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["conversation", "engine"])
async def test_failed_pre_teardown_join_retains_owned_storage(monkeypatch, failed):
    host = shutdown_method()()
    host.events = []
    async def conversation(_host):
        host.events.append("conversation")
        if failed == "conversation":
            raise RuntimeError("unjoined")
    async def engine(_host):
        host.events.append("engine")
        if failed == "engine":
            raise RuntimeError("unjoined")
    async def release():
        host.events.append("released")
    host._release_owned_storage = release
    monkeypatch.setattr(shutdown, "settle_before_shutdown", conversation)
    monkeypatch.setattr(native_engine_shutdown, "settle_before_shutdown", engine)
    with pytest.raises(RuntimeError, match="unjoined"):
        await host._shutdown()
    assert host._gui_quitting and "released" not in host.events and "base" not in host.events


@pytest.mark.asyncio
async def test_successful_pre_teardown_joins_still_release_storage_after_normal_teardown(monkeypatch):
    from litetui import launch_options, resource_session_lifecycle

    host = shutdown_method()()
    host.events = []
    async def conversation(_host):
        host.events.append("conversation")
    async def engine(_host):
        host.events.append("engine")
    async def settle():
        host.events.append("tree")
    async def release():
        host.events.append("released")
    host._settle_before_teardown = settle
    host._release_owned_storage = release
    monkeypatch.setattr(shutdown, "settle_before_shutdown", conversation)
    monkeypatch.setattr(native_engine_shutdown, "settle_before_shutdown", engine)
    monkeypatch.setattr(resource_session_lifecycle, "begin_shutdown", lambda _host: {})
    monkeypatch.setattr(launch_options, "stop_custom", lambda _host: None)
    await host._shutdown()
    assert host.events == ["conversation", "engine", "tree", "base", "released"]
