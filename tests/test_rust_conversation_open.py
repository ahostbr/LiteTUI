"""Owned conversation-open lifecycle required by the Rust browser; no provider."""
from __future__ import annotations

import asyncio
from inspect import unwrap
from types import SimpleNamespace as NS

import httpx
import pytest
import pytest_asyncio

from litetui import gui_rpc, hook_host, settings, store_identity
from litetui.conversation import ConversationRepository


@pytest_asyncio.fixture
async def saved_app(make_app, monkeypatch):
    # The owned constructor, _resume, authority/settings and disk reads are real.
    # Providers, workers and visual/fleet/hook observers are explicit controls.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("External invocation forbidden in conversation-open fixture")

    monkeypatch.setattr("platform.node", lambda: "controlled-host")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(hook_host, "enter_conversation", lambda *_: None)
    monkeypatch.setattr(hook_host, "leave_conversation", lambda *_: None)
    settings.save(settings.Settings(backend="codex", default_model="fixture-model",
                                    tools_enabled=False, mcp_enabled=False, skills_enabled=False,
                                    codex_native_engine=True))
    app = make_app(initial_backend="codex", initial_model="fixture-model")
    app._system = lambda *_: None
    app._refresh_ctx_label = lambda: None
    app._sync_fleet_identity = lambda: None
    app._render_resumed = lambda *_args, **_kwargs: None
    app._ensure_legacy_store_notice = lambda: None
    app._chat_running = lambda: False
    app.conversation = [{"role": "system", "content": "Fixture system"},
                        {"role": "user", "content": "Saved prompt"}]
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    try:
        yield app
    finally:
        await app.client.close()


class ConnectionGate:
    def __init__(self, app, *, native=True, failure=None, switch=None):
        self.app, self.native = app, native
        self.failure, self.switch = failure, switch
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.history_entered, self.history_release = asyncio.Event(), asyncio.Event()
        self.calls = []
        self.connection = NS(wait=self.wait_connection)
        self.history = NS(wait=self.wait_history)

    def connect(self):
        self.calls.append("connect scheduled")
        return self.connection

    def change_identity(self, phase):
        if self.switch and self.switch[0] == phase:
            field = self.switch[1]
            replacement = "different" if field in ("convo_id", "_model_id") else object()
            setattr(self.app, field, replacement)

    async def wait_connection(self):
        self.calls.append("connect waiting")
        assert gui_rpc.current_operation(self.app) == "open-owned"
        self.entered.set()
        await self.release.wait()
        self.change_identity("connect")
        if self.failure == "raised":
            raise OSError("controlled connection exception")
        self.app._connect_settled = True
        self.app._gui_connection_success = self.failure != "connection"
        self.app._resume_connection_error = (
            "controlled connection failed" if self.failure == "connection"
            else "saved model unavailable" if self.failure == "model" else None
        )
        if self.native:
            self.app._native_history_worker = self.history

    async def wait_history(self):
        self.calls.append("history waiting")
        self.history_entered.set()
        await self.history_release.wait()
        self.change_identity("history")
        if self.failure == "history":
            raise OSError("controlled history exception")
        if not self.switch or self.switch[0] != "history":
            self.app._append({"role": "assistant", "content": "Reconciled saved answer"})


def start_open(app, gate):
    app.connect = gate.connect
    return asyncio.create_task(gui_rpc.async_dispatch(app, {
        "type": "gui.conversations.open", "id": "open-owned", "session_id": app.convo_id,
    }))


async def reached(event, task):
    # Do not mistake a failed/early-returning RPC for a slow worker.
    wait = asyncio.create_task(event.wait())
    try:
        done, _ = await asyncio.wait({wait, task}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
        assert wait in done and event.is_set(), (
            f"Open finished before lifecycle wait: {task.exception() if task.done() else 'timeout'}"
        )
    finally:
        wait.cancel()
        await asyncio.gather(wait, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_owned_open_waits_for_connection_then_new_history_worker(saved_app, native):
    app = saved_app
    gate = ConnectionGate(app, native=native)
    before = (app._agent_session.memory_root / "settings.json").read_bytes()
    task = start_open(app, gate)
    try:
        await reached(gate.entered, task)
        assert not task.done()
        assert app._gui_management_busy
        with pytest.raises(ValueError, match="management operation"):
            await gui_rpc.async_dispatch(app, {"type": "gui.conversations.open",
                                              "session_id": app.convo_id})
        with pytest.raises(ValueError, match="management operation"):
            gui_rpc._idle(app)
        with pytest.raises(ValueError, match="management"):
            await gui_rpc.async_dispatch(app, {"type": "gui.prompt.submit", "message": "No send"})
        gate.release.set()
        if native:
            await reached(gate.history_entered, task)
            assert not task.done()
            assert app._gui_management_busy
            gate.history_release.set()
        result = await asyncio.wait_for(task, 1)
        assert result["session_id"] == app.convo_id
        assert result["read_only"] is False
        assert result["connected"] is True
        assert result["messages"][-1]["content"] == (
            "Reconciled saved answer" if native else "Saved prompt")
        assert result["messages"] == ConversationRepository.read(app.convo_path)[1]
        assert not app._gui_management_busy
        assert app._gui_management_owner is None
        assert (app._agent_session.memory_root / "settings.json").read_bytes() == before
        assert [m["content"] for m in app.conversation if m["role"] == "user"] == ["Saved prompt"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connection", "model", "raised", "history"])
async def test_open_failure_never_reports_connected_or_waits_for_unstarted_history(saved_app, failure):
    app = saved_app
    gate = ConnectionGate(app, failure=failure)
    gate.release.set()
    gate.history_release.set()
    with pytest.raises((ValueError, OSError), match="controlled|unavailable"):
        await start_open(app, gate)
    assert not app._gui_management_busy
    assert app._gui_management_owner is None
    assert ("history waiting" in gate.calls) == (failure == "history")
    assert app.convo_id == app.convo_path.parent.name  # No invented rollback.


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", [
    (phase, field) for phase in ("connect", "history")
    for field in ("conversation", "_backend", "convo_id", "_model_id", "_resume_connect_worker")
] + [("history", "_native_history_worker")])
async def test_open_rejects_identity_change_after_each_await(saved_app, switch):
    app = saved_app
    gate = ConnectionGate(app, switch=switch)
    gate.release.set()
    gate.history_release.set()
    with pytest.raises(ValueError, match="Conversation changed"):
        await start_open(app, gate)
    assert not app._gui_management_busy
    assert ("history waiting" in gate.calls) == (switch[0] == "history")


@pytest.mark.asyncio
async def test_open_cancellation_releases_management_busy(saved_app):
    gate = ConnectionGate(saved_app)
    task = start_open(saved_app, gate)
    try:
        await reached(gate.entered, task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not saved_app._gui_management_busy
        assert saved_app._gui_management_owner is None
        assert not gate.history_entered.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_open_same_id_must_respect_failed_resume(saved_app):
    app = saved_app
    # A valid meta record with no messages makes the actual _resume return False.
    app.convo_path.write_text('{"type":"meta","id":"' + app.convo_id + '"}\n', encoding="utf-8")
    before = app.convo_path.read_bytes()
    with pytest.raises(ValueError, match="could not be resumed"):
        await gui_rpc.async_dispatch(app, {"type": "gui.conversations.open",
                                          "session_id": app.convo_id})
    assert app.convo_path.read_bytes() == before
    assert not app._gui_management_busy


@pytest.mark.asyncio
async def test_open_cannot_adopt_stale_success_without_a_resume_worker(saved_app):
    app = saved_app
    app._gui_connection_success = True
    app._connect_settled = True
    app.connect = lambda: None
    with pytest.raises(ValueError, match="connection"):
        await gui_rpc.async_dispatch(app, {"type": "gui.conversations.open",
                                          "session_id": app.convo_id})
    assert not app._gui_management_busy


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", ["available", "missing"])
async def test_open_waits_through_actual_connection_body(saved_app, monkeypatch, catalog):
    from litetui import launch_options, llm_backend
    from litetui.app import LiteTUI

    app = saved_app
    backend = app.backend
    entered, release = asyncio.Event(), asyncio.Event()
    tasks = []
    emitted = []

    async def prepared(_app):
        return "ok"

    async def listed():
        entered.set()
        await release.wait()
        key = "fixture-model" if catalog == "available" else "other-model"
        return [llm_backend.ModelRow(key=key, path=None, source="fixture", loaded=True)]

    async def refreshed():
        app._append({"role": "assistant", "content": "History after actual connect"})

    def worker(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        async def wait():
            return await task
        return NS(wait=wait)

    monkeypatch.setattr(launch_options, "prepare", prepared)
    monkeypatch.setattr(llm_backend, "make_backend", lambda _settings: backend)
    monkeypatch.setattr(backend, "list_models", listed)
    app._update_header = lambda: None
    app._fetch_ctx_window = lambda: None
    app._rpc_emit_model_state = lambda: emitted.append(gui_rpc.current_operation(app))
    app._refresh_native_history = lambda _path: worker(refreshed())
    app.connect = lambda: worker(unwrap(LiteTUI.connect)(app))
    task = asyncio.create_task(gui_rpc.async_dispatch(app, {
        "type": "gui.conversations.open", "session_id": app.convo_id, "id": "actual-connect",
    }))
    try:
        await reached(entered, task)
        assert not task.done()
        assert app._gui_management_busy
        release.set()
        if catalog == "missing":
            with pytest.raises(ValueError, match="unavailable"):
                await task
            assert not emitted
        else:
            result = await task
            assert result["connected"] is True
            assert result["messages"][-1]["content"] == "History after actual connect"
            assert emitted == ["actual-connect"]
        assert app._connect_settled
        assert not app._gui_management_busy
    finally:
        for pending in [task, *tasks]:
            pending.cancel()
        await asyncio.gather(task, *tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "connection"])
async def test_rpc_open_response_is_delayed_and_legacy_prompt_is_blocked(saved_app, monkeypatch, failure):
    from litetui import rpc

    app = saved_app
    gate = ConnectionGate(app, failure=failure)
    app.connect = gate.connect
    frames: list[dict] = []
    tasks = []

    def run(coroutine, **kwargs):
        assert kwargs == {"group": "gui-management", "exit_on_error": False}
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        return task

    def submitted(*_args, **_kwargs):
        raise AssertionError("Prompt reached submission during conversation open")

    app.run_worker = run
    app._submit_text = submitted
    monkeypatch.setattr(rpc, "rpc_emit", frames.append)
    rpc._dispatch(app, {"type": "gui.conversations.open", "id": "open-owned",
                        "session_id": app.convo_id})
    try:
        await reached(gate.entered, tasks[0])
        assert not frames
        rpc._dispatch(app, {"type": "prompt", "id": "too-early", "message": "No provider"})
        assert len(frames) == 1 and frames[0]["id"] == "too-early"
        assert frames[0]["ok"] is False and "management" in frames[0]["error"]
        gate.release.set()
        if failure is None:
            await reached(gate.history_entered, tasks[0])
            assert len(frames) == 1
            gate.history_release.set()
        await tasks[0]
        assert len(frames) == 2 and frames[1]["id"] == "open-owned"
        assert frames[1]["ok"] is (failure is None)
        if failure is None:
            assert frames[1]["result"]["connected"] is True
            assert frames[1]["result"]["messages"][-1]["content"] == "Reconciled saved answer"
        else:
            assert "controlled connection failed" in frames[1]["error"]
        assert not app._gui_management_busy
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
