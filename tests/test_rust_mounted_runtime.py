"""Bounded mounted runtime with scripted external transports, not a live child.

The fixture catalog loads real core_tools and self_compact; other plugins and
stdin-reader/updater startup and thinking-level discovery are excluded. Model,
backend HTTP, hook process and registration transports are scripted, not tested.
Real hook selection/authorization/parser, admission, queue, engine, backend
readiness orchestration and owned storage remain in the exercised path.
Suite fixtures redirect home/data/config before this scope; not clean-import or
OS containment. Every unexpected controlled transport attempt is recorded.
"""
from __future__ import annotations

import asyncio
import copy
import io
import json
import platform
import socket
import subprocess
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace as NS

import httpx
import pytest
from test_rust_owned_construction import ConstructionGate

from litetui import (
    agent_launch_context,
    approval_relay,
    gui_rpc,
    harness,
    hook_host,
    llm_backend,
    mcp_client,
    model_transport,
    paths,
    plugins,
    rpc,
    settings,
    thinking_probe,
    ttyguard,
    update_check,
)
from litetui.app import LiteTUI
from litetui.conversation import ConversationRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]
MODEL = "mounted-fixture-model"
AID = "44444444-4444-4444-8444-444444444444"
NAME = "RustMountedFixture"
WORK_OWNER = ContextVar("rust_mounted_fixture_owner", default=None)


class HookProcess:
    """In-memory pipes; real hook runner still feeds/drains/parses and joins."""
    def __init__(self, documents, fail_event):
        process = self
        self.document = None
        self.fail_event = fail_event

        class Input(io.StringIO):
            def close(self):
                if not self.closed:
                    process.document = json.loads(self.getvalue())
                    documents.append(process.document)
                super().close()

        self.stdin = Input()
        self.stdout = io.StringIO('{"decision":"allow"}\n')
        self.stderr = io.StringIO("")
        self.pid = -1  # No process was created; kill must never be attempted.

    def poll(self):
        return 7 if self.document and self.document["event"] == self.fail_event else 0


class ScriptedStream:
    def __init__(self, delta, finish):
        self.delta = delta
        self.finish = finish
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.close()

    def __aiter__(self):
        async def chunks():
            yield NS(choices=[NS(delta=self.delta, finish_reason=self.finish)], usage=None)
        return chunks()

    async def close(self):
        self.closed = True


class MountedRuntime:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.monkeypatch = monkeypatch
        # Reuse only the low-level tripwire/cleanup recorder, not its
        # constructor-only open(), hook stub or registration substitution.
        self.effects = ConstructionGate(root, monkeypatch)
        self.records = []
        self.requests = []
        self.streams = []
        self.discovery = []
        self.hook_documents = []
        self.hook_processes = []
        self.fail_hook_event = None
        self.readers = []
        self.registrations = []
        self.ready = asyncio.Event()
        self.tool_requested = asyncio.Event()
        self.ended = asyncio.Event()
        self.app = None
        self.tasks = []
        self.workers = []
        self.hook_outcomes = []
        self.cleanup_errors = []
        self.problems = []
        self.excluded_updates = []
        self.excluded_thinking = []

    def cleanup(self, label, callback):
        try:
            self.effects.cleanup(label, callback)
        except BaseException as exc:
            self.cleanup_errors.append((label, type(exc).__name__, str(exc)))
            raise

    async def cleanup_async(self, label, callback):
        try:
            await self.effects.cleanup_async(label, callback)
        except BaseException as exc:
            self.cleanup_errors.append((label, type(exc).__name__, str(exc)))
            raise

    def observe_work(self, stack, loop, patch):
        previous_factory = loop.get_task_factory()
        owner_token = WORK_OWNER.set(self)
        stack.callback(WORK_OWNER.reset, owner_token)

        def restore_factory():
            if loop.get_task_factory() is self.task_factory:
                loop.set_task_factory(previous_factory)
            # A replacement belongs to another owner. Verification reports it;
            # restoring our predecessor here would silently clobber that owner.

        stack.callback(restore_factory)

        def factory(loop, coroutine, context=None, **kwargs):
            if previous_factory is None:
                task = asyncio.Task(coroutine, loop=loop, context=context, **kwargs)
            elif context is None:
                task = previous_factory(loop, coroutine, **kwargs)
            else:
                task = previous_factory(loop, coroutine, context=context, **kwargs)
            owner = context.get(WORK_OWNER) if context is not None else WORK_OWNER.get()
            if owner is self:
                self.tasks.append(task)
            return task

        self.task_factory = factory
        loop.set_task_factory(factory)
        run_worker = LiteTUI.run_worker

        def observed_worker(app, *args, **kwargs):
            worker = run_worker(app, *args, **kwargs)
            if app is self.app:
                self.workers.append(worker)
            return worker

        patch.setattr(LiteTUI, "run_worker", observed_worker)
        invoke = hook_host.invoke

        async def observed_invoke(app, hook, document, *args, **kwargs):
            try:
                result = await invoke(app, hook, document, *args, **kwargs)
            except BaseException as exc:
                self.hook_outcomes.append((document["event"], False, type(exc).__name__))
                raise
            self.hook_outcomes.append((document["event"], result.allowed, result.reason))
            return result

        patch.setattr(hook_host, "invoke", observed_invoke)

    async def settle_owned_work(self):
        # Includes tasks born during unmount, not only Textual workers. Ownership
        # is inherited from this scope, not guessed from all_tasks()/task names.
        if asyncio.get_running_loop().get_task_factory() is not self.task_factory:
            raise AssertionError("fixture task factory replaced before settlement")
        deadline = asyncio.get_running_loop().time() + 5
        for _ in range(2):
            await asyncio.sleep(0)
            pending = {task for task in self.tasks if not task.done()}
            while pending:
                remaining = max(0, deadline - asyncio.get_running_loop().time())
                _, pending = await asyncio.wait(pending, timeout=remaining)
                if pending:
                    for task in pending:
                        task.cancel()
                    await asyncio.wait(pending, timeout=2)
                    raise AssertionError("fixture-owned work did not settle")
                pending = {task for task in self.tasks if not task.done()}

    async def wait(self, event):
        try:
            await asyncio.wait_for(event.wait(), 10)
        except TimeoutError:
            pytest.fail(f"runtime timeout: wire={[row.get('type') for row in self.records]!r}; "
                        f"requests={[row['purpose'] for row in self.requests]!r}; "
                        f"denied={self.effects.denied!r}; hooks={self.app.hook_results!r}; "
                        f"hook_config={hook_host.snapshot(self.app)!r}")

    async def create(self, transport, *, purpose="turn", retry_notice=None, **request):
        assert transport.client is self.app.client
        assert transport.backend is self.app.backend
        self.requests.append({"purpose": purpose, **copy.deepcopy(request)})
        if purpose == "card-summary":
            return NS(choices=[NS(message=NS(content="Fixture completed"))])
        if purpose != "turn":
            self.effects.forbid(f"unplanned model purpose: {purpose}")()
        count = sum(row["purpose"] == "turn" for row in self.requests)
        if count == 1:
            delta = NS(content=None, reasoning_content=None, tool_calls=[NS(
                index=0, id="fixture-tool-call", type="function",
                function=NS(name="fixture_roundtrip", arguments='{"value":"A"}'))])
            finish = "tool_calls"
        elif count == 2:
            delta = NS(content="Fixture final answer", reasoning_content=None, tool_calls=None)
            finish = "stop"
        else:
            self.effects.forbid(f"unplanned turn request: {count}")()
        stream = ScriptedStream(delta, finish)
        self.streams.append(stream)
        return stream

    async def start_tool_turn(self):
        app = self.app
        await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "hello", "protocol_version": 1})
        spec = {"type": "function", "function": {"name": "fixture_roundtrip",
                "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}
        await gui_rpc.async_dispatch(app, {"type": "gui.host_tools.register", "id": "register",
                                          "plugin_id": "fixture", "tools": [spec]})
        await gui_rpc.async_dispatch(app, {"type": "gui.prompt.submit", "id": "operation-A",
                                          "message": "First prompt", "behavior": "queue"})
        await self.wait(self.tool_requested)
        return next(row for row in self.records if row.get("type") == "host_tool_requested")

    @asynccontextmanager
    async def open(self):
        loop = asyncio.get_running_loop()
        runtime = self

        class Wire(io.StringIO):
            def write(self, text):
                result = super().write(text)
                row = json.loads(text)
                runtime.records.append(row)
                signal = {"ready": runtime.ready, "host_tool_requested": runtime.tool_requested,
                          "turn_end": runtime.ended}.get(row.get("type"))
                if signal is not None:
                    loop.call_soon_threadsafe(signal.set)
                return result

        with self.monkeypatch.context() as patch:
            workspace = self.root / "workspace"
            hook_dir = workspace / ".litetui"
            hook_dir.mkdir(parents=True)
            patch.chdir(workspace)
            # Explicitly strengthen conftest's already-temporary hook-path seam.
            patch.setattr(hook_host.hooks, "config_paths", lambda _workspace: (
                self.root / "global-hooks.json", hook_dir / "hooks.json"))
            patch.setattr(update_check, "start_background_check",
                          lambda app: self.excluded_updates.append(app))

            def thinking_levels(host, model, seed_models=()):
                # This fixture measures turn/origin/ownership, not autonomous
                # model capability probes. Keep the real worker and tripwires.
                if host != "http://127.0.0.1:1234" or model != MODEL:
                    self.effects.forbid("unplanned thinking-level discovery")()
                self.excluded_thinking.append((host, model, tuple(seed_models)))

            patch.setattr(thinking_probe, "get_effective_levels", thinking_levels)
            patch.setattr(platform, "node", lambda: "synthetic-host")
            for obj, name in (
                (subprocess, "Popen"), (socket, "create_connection"),
                (socket, "getaddrinfo"), (socket.socket, "connect"),
                (socket.socket, "connect_ex"), (httpx.Client, "send"),
                (mcp_client.MCPManager, "load"), (mcp_client.MCPManager, "reload_configs"),
                (mcp_client.MCPManager, "connect"), (llm_backend.LMStudioBackend, "_sdk"),
            ):
                patch.setattr(obj, name, self.effects.forbid(f"{obj.__name__}.{name}"))
            patch.setattr(httpx.AsyncClient, "send", self.effects.forbid_async("AsyncClient.send"))
            patch.setattr(plugins, "PLUGIN_LOAD_ORDER", (
                "litetui.plugins.core_tools", "litetui.plugins.self_compact"))
            patch.setattr(rpc, "start_rpc_reader", lambda app: self.readers.append(app))
            patch.setattr(rpc, "_real_stdout", Wire())
            for key in (harness.SPAWN_IDENTITY_MARKER, harness.OWNER_MARKER,
                        approval_relay.SPAWNED_BY_ENV, approval_relay.APPROVAL_HOST_ENV,
                        "LITETUI_HOOKS"):
                patch.delenv(key, raising=False)
            patch.setenv("LITETUI_BACKEND", "lmstudio")
            patch.setenv("LITETUI_DISABLE_UPDATE_CHECK", "1")
            patch.delenv("LITETUI_MODEL", raising=False)
            patch.delenv("LITETUI_THINKING", raising=False)

            def native_json(url, **_kwargs):
                if url != "http://127.0.0.1:1234/api/v0/models":
                    self.effects.forbid(f"unplanned backend request: {url}")()
                self.discovery.append(url)
                return {"data": [{"id": MODEL, "type": "llm", "loaded_context_length": 32768}]}

            def register(seat):
                self.registrations.append(seat.agent_id)
                seat.registered = True
                seat.error = ""
                return True

            async def create(transport, **kwargs):
                return await self.create(transport, **kwargs)

            def hook_process(command, **_kwargs):
                if command != [sys.executable, "-c", "pass"]:
                    self.effects.forbid(f"unplanned process: {command}")()
                process = HookProcess(self.hook_documents, self.fail_hook_event)
                self.hook_processes.append(process)
                return process

            patch.setattr(ttyguard, "popen", hook_process)
            patch.setattr(ttyguard, "kill_tree", self.effects.forbid("kill_tree"))
            patch.setattr(llm_backend, "_http_json", native_json)
            patch.setattr(harness.Seat, "register", register)
            patch.setattr(model_transport.OpenAITransport, "create", create)
            original_init = LiteTUI.__init__

            def observed_init(app, *args, **kwargs):
                self.app = app
                original_init(app, *args, **kwargs)

            patch.setattr(LiteTUI, "__init__", observed_init)
            settings.save(settings.Settings(
                backend="lmstudio", lm_host="http://127.0.0.1:1234",
                skills_enabled=False, mcp_enabled=False, footer_task_manager=False,
                tts_enabled=False, sidecar_enabled=False, tool_policy_profile="autonomous",
                tool_auto_background_s=0,
            ), root=self.root)
            global_before = settings.settings_path().read_bytes()
            assert paths.data_root() == self.root
            agent_settings = agent_before = None

            def verify(_exc_type, active_error, _traceback):
                # Runs after resource cleanup, before task-factory/transport
                # restoration, including when the body or a cleanup failed.
                problems = list(self.effects.denied)
                if loop.get_task_factory() is not self.task_factory:
                    problems.append("fixture task factory replaced")
                problems.extend(f"cleanup:{label}:{kind}:{detail}"
                                for label, kind, detail in self.cleanup_errors)
                problems.extend(f"hook:{event}:{reason}" for event, allowed, reason in self.hook_outcomes
                                if not allowed)
                for task in self.tasks:
                    if not task.done():
                        problems.append("fixture task still running")
                    elif not task.cancelled() and task.exception() is not None:
                        problems.append(f"task:{type(task.exception()).__name__}:{task.exception()}")
                for worker in self.workers:
                    if not worker.is_finished:
                        problems.append(f"worker still running:{worker.name}")
                    if worker.error is not None and not isinstance(worker.error, asyncio.CancelledError):
                        problems.append(f"worker:{worker.name}:{type(worker.error).__name__}")
                if getattr(self.app, "_hook_lifecycle", []):
                    problems.append("lifecycle backlog left pending")
                if settings.settings_path().read_bytes() != global_before:
                    problems.append("global settings changed")
                if agent_settings is not None and agent_settings.read_bytes() != agent_before:
                    problems.append("agent settings changed")
                if (self.root / ".convos").exists():
                    problems.append("legacy transcript directory created")
                if not all(stream.closed for stream in self.streams):
                    problems.append("model stream left open")
                if not all(pipe.closed for proc in self.hook_processes
                           for pipe in (proc.stdin, proc.stdout, proc.stderr)):
                    problems.append("hook pipe left open")
                if getattr(self.app, "_gui_host_pending", {}):
                    problems.append("host requests left pending")
                assigned = vars(self.app) if self.app is not None else {}
                if assigned.get("client") is not None and not assigned["client"].is_closed():
                    problems.append("client left open")
                self.problems = problems
                if problems:
                    failure = AssertionError(problems)
                    if active_error is not None:
                        # A negative test expecting ONLY a boundary assertion
                        # must not consume an unrelated body/settlement error.
                        raise BaseExceptionGroup("mounted runtime failures", [active_error, failure])
                    raise failure

            async with AsyncExitStack() as stack:
                self.observe_work(stack, loop, patch)
                stack.push(verify)
                session = agent_launch_context.create(
                    self.root, NAME, agent_id=AID, backend="lmstudio", model=MODEL,
                    thinking_level="default",
                )
                stack.callback(self.cleanup, "session", session.release)
                agent_settings = session.memory_root / "settings.json"
                agent_before = agent_settings.read_bytes()
                try:
                    app = LiteTUI(agent_session=session, rpc=True)
                    assert app._hook_workspace == workspace.resolve()
                    assert app.hook_config.project_path.resolve().is_relative_to(workspace.resolve())
                    app.hook_config.project_path.write_text(json.dumps({"version": 1, "hooks": [
                        {"id": "lifecycle", "mode": "observe", "events": ["app_start", "app_shutdown",
                         "conversation_start", "conversation_leave"], "executable": sys.executable,
                         "argv": ["-c", "pass"]},
                        {"id": "admission", "mode": "gate", "events": ["prompt_before", "tool_before",
                         "completion_before"], "executable": sys.executable, "argv": ["-c", "pass"]},
                    ]}), encoding="utf-8")
                    assert not hook_host.snapshot(app).error
                    async with asyncio.timeout(45), app.run_test(size=(110, 40)) as pilot:
                        try:
                            await self.wait(self.ready)
                            await asyncio.wait_for(hook_host.drain_lifecycle(app), 10)
                            yield app, pilot
                        finally:
                            for request_id in list(getattr(app, "_gui_host_pending", {})):
                                gui_rpc.dispatch(app, {"type": "gui.host_tools.result",
                                    "request_id": request_id, "error": "fixture teardown"})
                            await asyncio.wait_for(app.workers.wait_for_complete(), 10)
                finally:
                    try:
                        await self.settle_owned_work()
                    finally:
                        assigned = vars(self.app) if self.app is not None else {}
                        backend, client, store = (assigned.get(key) for key in ("_backend", "client", "_store"))
                        if backend is not None:
                            stack.callback(self.cleanup, "backend", backend.shutdown)
                        if client is not None:
                            stack.push_async_callback(self.cleanup_async, "client", client.close)
                        if store is not None:
                            stack.callback(self.cleanup, "store", store.release)


async def test_mounted_ready_uses_real_connect_hooks_and_owned_shutdown(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    async with runtime.open() as (app, _pilot):
        ready = next(row for row in runtime.records if row.get("type") == "ready")
        assert ready["launch_status"] == "ready", (
            ready.get("launch_error"), ready.get("model_note"), runtime.effects.denied, runtime.discovery)
        assert ready["tools_enabled"] is app.tools_enabled
        assert ready["mcp_enabled"] is app.settings.mcp_enabled
        from litetui.seat_authority import confirm_route
        assert ready["approval_route"] == confirm_route(app)
        assert app._gui_connection_success
        assert app.model_id == MODEL
        assert runtime.discovery
        assert runtime.registrations == [AID]
        assert runtime.readers == runtime.excluded_updates == [app]
        assert not hook_host.snapshot(app).error
        assert [doc["event"] for doc in runtime.hook_documents] == ["app_start"]
        assert app._hook_lifecycle == []
        assert type(app.store) is ConversationRepository
        assert not app.store.owned
    assert app._owned_storage_released
    assert app.client.is_closed()
    with agent_launch_context.acquire(tmp_path, NAME) as reopened:
        assert reopened.authority.agent_id == AID


async def test_real_turn_consumes_queued_prompt_after_host_tool_result(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    async with runtime.open() as (app, _pilot):
        requested = await runtime.start_tool_turn()
        assert requested["operation_id"] == "operation-A"
        assert app._chat_running()
        await gui_rpc.async_dispatch(app, {"type": "gui.images.clear", "id": "management"})
        await gui_rpc.async_dispatch(app, {"type": "gui.prompt.submit", "id": "operation-B",
                                          "message": "Queued prompt", "behavior": "queue"})
        assert len(app._pending_input) == 1
        assert app._pending_input[0]["operation_id"] == "operation-B"
        assert app._gui_operation_id == "operation-A"
        await gui_rpc.async_dispatch(app, {"type": "gui.host_tools.result", "id": "result",
            "request_id": requested["request_id"], "result": "Fixture tool result"})
        await runtime.wait(runtime.ended)
        await asyncio.wait_for(app.workers.wait_for_complete(), 10)
        turns = [row for row in runtime.requests if row["purpose"] == "turn"]
        assert len(turns) == 2
        messages = turns[1]["messages"]
        queued = [i for i, message in enumerate(messages)
                  if message.get("role") == "user" and message.get("content") == "Queued prompt"]
        tool_results = [i for i, message in enumerate(messages)
                        if message.get("role") == "tool" and message.get("tool_call_id") == "fixture-tool-call"]
        assert len(queued) == len(tool_results) == 1
        assert tool_results[0] < queued[0]
        assert not app._pending_input
        terminal = [row for row in runtime.records if row.get("type") == "gui.event"
                    and row["event"].get("type") == "turn_end"]
        assert len(terminal) == 1
        assert terminal[0]["operation_id"] == "operation-A"
        assert terminal[0]["event"]["stopReason"] == "stop"
        transcript = app.store.convo_path
        assert app.store.persist_error is None
        meta, durable = ConversationRepository.read(transcript)
        assert meta["agent_id"] == AID
        assert sum(message.get("content") == "Queued prompt" for message in durable) == 1
        assert durable[-1]["content"] == "Fixture final answer"
        admitted = [doc for doc in runtime.hook_documents if doc["event"] == "prompt_before"]
        assert [doc["data"]["prompt"] for doc in admitted] == ["First prompt", "Queued prompt"]
        assert len({doc["turn_id"] for doc in admitted}) == 2
        assert all(doc["conversation_id"] == app.convo_id for doc in admitted)
        assert any(doc["event"] == "tool_before" for doc in runtime.hook_documents)
        assert any(doc["event"] == "completion_before" for doc in runtime.hook_documents)
    assert list(tmp_path.rglob("convo.jsonl")) == [transcript]
    lifecycle = [doc["event"] for doc in runtime.hook_documents if doc["event"] in (
        "app_start", "conversation_start", "conversation_leave", "app_shutdown")]
    assert lifecycle == ["app_start", "conversation_start", "conversation_leave", "app_shutdown"]


async def test_failure_while_host_tool_is_held_still_settles_and_releases(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="test body failed"):
        async with runtime.open() as (app, _pilot):
            requested = await runtime.start_tool_turn()
            assert requested["request_id"] in app._gui_host_pending
            raise RuntimeError("test body failed")
    assert not app._gui_host_pending
    assert app.client.is_closed()
    assert app._owned_storage_released
    assert all(worker.is_finished for worker in app.workers)
    assert runtime.effects.cleaned == ["store", "client", "backend", "session"]
    assert any(doc["event"] == "app_shutdown" for doc in runtime.hook_documents)
    with agent_launch_context.acquire(tmp_path, NAME) as reopened:
        assert reopened.authority.agent_id == AID


async def test_swallowed_external_attempt_fails_after_mounted_cleanup(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    with pytest.raises(AssertionError, match="getaddrinfo"):
        async with runtime.open() as (app, _pilot):
            try:
                socket.getaddrinfo("fixture.invalid", 80)
            except AssertionError:
                pass  # The durable ledger, not this immediate exception, must fail.
    assert runtime.problems == runtime.effects.denied == ["socket.getaddrinfo"]
    assert runtime.cleanup_errors == []
    assert app.client.is_closed()
    assert app._owned_storage_released
    assert all(worker.is_finished for worker in app.workers)
    with agent_launch_context.acquire(tmp_path, NAME) as reopened:
        assert reopened.authority.agent_id == AID


async def test_expected_denial_cannot_hide_cleanup_failure(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    with pytest.raises(ExceptionGroup) as caught:
        async with runtime.open() as (app, _pilot):
            close = app.client.close

            async def fail_after_close():
                await close()
                raise RuntimeError("fixture close failed")

            monkeypatch.setattr(app.client, "close", fail_after_close)
            try:
                socket.getaddrinfo("fixture.invalid", 80)
            except AssertionError:
                pass
    assert [type(exc) for exc in caught.value.exceptions] == [RuntimeError, AssertionError]
    assert str(caught.value.exceptions[0]) == "fixture close failed"
    assert runtime.problems == ["socket.getaddrinfo", "cleanup:client:RuntimeError:fixture close failed"]
    assert runtime.cleanup_errors == [("client", "RuntimeError", "fixture close failed")]
    assert runtime.effects.cleaned == ["store", "client", "backend", "session"]
    assert app.client.is_closed()
    with agent_launch_context.acquire(tmp_path, NAME) as reopened:
        assert reopened.authority.agent_id == AID


async def test_task_created_during_unmount_runs_under_tripwires(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    unmount = LiteTUI.on_unmount
    late_tasks = []

    async def unmount_then_schedule(app):
        await unmount(app)

        async def late_attempt():
            await asyncio.sleep(0)
            try:
                socket.getaddrinfo("fixture.invalid", 80)
            except AssertionError:
                pass

        late_tasks.append(asyncio.create_task(late_attempt()))

    monkeypatch.setattr(LiteTUI, "on_unmount", unmount_then_schedule)
    with pytest.raises(AssertionError, match="getaddrinfo"):
        async with runtime.open():
            pass
    assert runtime.problems == ["socket.getaddrinfo"]
    assert len(late_tasks) == 1
    assert late_tasks[0] in runtime.tasks and late_tasks[0].done()
    assert all(task.done() for task in runtime.tasks)
    assert runtime.cleanup_errors == []


async def test_failed_shutdown_observer_is_not_successful_delivery(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    runtime.fail_hook_event = "app_shutdown"
    with pytest.raises(AssertionError, match="hook:app_shutdown"):
        async with runtime.open():
            pass
    assert runtime.problems == ["hook:app_shutdown:hook exited with 7"]
    assert runtime.hook_outcomes[-1] == ("app_shutdown", False, "hook exited with 7")
    assert runtime.cleanup_errors == []


@pytest.mark.parametrize("phase", ["body", "settlement"])
async def test_expected_denial_cannot_hide_scope_failure(tmp_path, monkeypatch, phase):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    settle = runtime.settle_owned_work

    async def fail_after_settlement():
        await settle()
        raise RuntimeError("unexpected settlement failure")

    if phase == "settlement":
        monkeypatch.setattr(runtime, "settle_owned_work", fail_after_settlement)
    with pytest.raises(ExceptionGroup) as caught:
        async with runtime.open():
            try:
                socket.getaddrinfo("fixture.invalid", 80)
            except AssertionError:
                pass
            if phase == "body":
                raise RuntimeError("unexpected body failure")
    assert [type(exc) for exc in caught.value.exceptions] == [RuntimeError, AssertionError]
    assert str(caught.value.exceptions[0]) == f"unexpected {phase} failure"
    assert caught.value.exceptions[1].args == (["socket.getaddrinfo"],)
    assert runtime.problems == ["socket.getaddrinfo"]
    assert runtime.cleanup_errors == []
    assert all(task.done() for task in runtime.tasks)


async def test_task_factory_replacement_is_reported_without_clobbering_owner(tmp_path, monkeypatch):
    runtime = MountedRuntime(tmp_path, monkeypatch)
    loop = asyncio.get_running_loop()
    original = loop.get_task_factory()

    def sentinel(loop, coroutine, context=None, **kwargs):
        return asyncio.Task(coroutine, loop=loop, context=context, **kwargs)

    try:
        with pytest.raises(ExceptionGroup) as caught:
            async with runtime.open():
                loop.set_task_factory(sentinel)
        assert loop.get_task_factory() is sentinel
        assert [type(exc) for exc in caught.value.exceptions] == [AssertionError, AssertionError]
        assert str(caught.value.exceptions[0]) == "fixture task factory replaced before settlement"
        assert runtime.problems == ["fixture task factory replaced"]
        assert runtime.cleanup_errors == []
    finally:
        # This test owns the sentinel; the fixture correctly left it alone.
        loop.set_task_factory(original)
