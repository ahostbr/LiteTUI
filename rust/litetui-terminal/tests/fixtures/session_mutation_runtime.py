"""Actual owned Rename/New subprocess with only external boundaries controlled."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_SESSION_MUTATION_SOURCE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ["RUST_BACKEND_SWITCH_SOURCE"] = str(source)
os.environ["RUST_BACKEND_SWITCH_CASE"] = "success"
spec = importlib.util.spec_from_file_location(
    "_session_external_boundaries", Path(__file__).with_name("backend_switch_runtime.py"))
assert spec and spec.loader
boundary: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
case = os.environ.get("RUST_SESSION_MUTATION_CASE", "rename-saved")
driver = os.environ.get("RUST_SESSION_MUTATION_DRIVER", "self")
phase = os.environ.get("RUST_SESSION_MUTATION_PHASE", "fresh")
action = os.environ.get("RUST_SESSION_MUTATION_ACTION", "new" if case.startswith("new-") else "rename")
title = os.environ.get("RUST_SESSION_MUTATION_TITLE", "  Renamed  雪\ncurrent\tconversation  ")
requests: list[dict] = []
events: list[dict] = []
pre_requests: list[dict] = []
sessions: list[Any] = []
codex_calls: list[dict] = []
codex_threads: list[str] = []
applied = False
apply_target = None
stale_used = False
closing_cases = {"quit-during-apply", "eof-during-apply", "kill-refused"}
native_closing_cases = {"claude-quit-during-apply", "claude-eof-during-apply"}
all_closing_cases = closing_cases | native_closing_cases
hook_entered = threading.Event()
hook_processes: list[Any] = []
hook_cases = closing_cases | {"hooks-success", "hooks-failed-leave", "hooks-failed-start", "hooks-approval", "hooks-drift", "codex-correction"}


async def inference(transport, *, purpose="turn", **request):
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled session summary"))])
    app = boundary.app_instance
    assert purpose == "turn" and app is not None
    assert transport.backend is app.backend
    authority = app._agent_session.authority
    assert request["model"] == authority.model and transport.backend.name == authority.backend
    requests.append({"session_id": app.convo_id, "agent_id": authority.agent_id,
                     "backend": transport.backend.name, "model": request["model"],
                     "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled,
                     "messages": copy.deepcopy(request["messages"])})
    stream = boundary.Stream()
    boundary.streams.append(stream)
    return stream


def disk():
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()
            and not path.name.endswith((".lock", ".lease"))
            and not path.name.startswith("session-mutation-")}


def current(app):
    return {**boundary.identity(app), "pending": app.store.pending, "owned": app.store.owned,
            "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled,
            "history": copy.deepcopy(app.conversation), "queue_count": len(app._pending_input),
            "directory": str(app.convo_dir), "path": str(app.convo_path)}


async def settle(app):
    from textual.worker import WorkerCancelled

    from litetui import hook_host
    await hook_host.drain_lifecycle(app)
    async def workers():
        for _ in range(6):
            current_workers = list(app.workers)
            results = await asyncio.gather(*(worker.wait() for worker in current_workers), return_exceptions=True)
            for worker, result in zip(current_workers, results, strict=True):
                if isinstance(result, WorkerCancelled) and (worker.group != "chat" or worker.name != "_stream"):
                    # Canonical hook admission hands off to an exclusive _stream,
                    # which retires the admission coroutine in the same group.
                    events.append({"effect": "worker-retired", "name": worker.name, "group": worker.group})
                elif isinstance(result, BaseException):
                    raise result
            await asyncio.sleep(0)
            if not list(app.workers):
                return
        raise AssertionError("Owned workers did not retire")
    await asyncio.wait_for(workers(), 8)
    await asyncio.sleep(0)


class WriteProbe:
    def __init__(self, stream):
        self.stream = stream

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()
        events.append({"effect": "durable-write-before-error"})
        raise OSError("Controlled lost transcript write acknowledgement")


async def codex_start(server):
    if server.process is None:
        server.process = NS(returncode=None)
        # Native policy callback transport is an external boundary in this fixture.
        server.native_bridge = NS(policy=None)
    server.initialized = True


async def codex_close(server):
    if server.process is not None:
        server.process.returncode = 0
    server.initialized = False
    server.runtime_activity.disconnected()


async def codex_request(server, method, params):
    codex_calls.append({"method": method, "params": copy.deepcopy(params)})
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        return {"data": [{"model": "gpt-6.1-sol", "displayName": "Controlled Codex",
                          "defaultReasoningEffort": "high",
                          "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
    if method == "thread/start":
        thread = "controlled-thread-" + str(len(codex_threads))
        codex_threads.append(thread)
        return {"thread": {"id": thread}}
    if method == "turn/start":
        app = boundary.app_instance
        assert params["threadId"] in codex_threads
        requests.append({"session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
                         "backend": "codex", "model": params["model"],
                         "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled,
                         "thread_id": params["threadId"], "input": copy.deepcopy(params["input"])})
        turn = "controlled-turn-" + str(len(pre_requests) + len(requests))
        for event in [
            {"method": "item/agentMessage/delta", "params": {"threadId": params["threadId"], "turnId": turn,
             "itemId": "answer-" + turn, "delta": "Controlled Codex answer"}},
            {"method": "turn/completed", "params": {"threadId": params["threadId"],
             "turn": {"id": turn, "status": "completed"}}},
        ]:
            await server.events.put(event)
        return {"turn": {"id": turn}}
    # Neither Rename nor fresh New may resume the old provider thread.
    raise AssertionError("Unexpected provider request: " + method)


class NativeSession:
    """Synthetic SDK boundary; all Claude backend/ledger/turn logic stays real."""
    def __init__(self, options):
        self.options = options
        self.session_id = "controlled-session-" + str(len(sessions))
        self.lifecycle = NS(cleanup_errors=[], failure=None, active_turn=False)
        self.effort = None
        self.queries = 0
        sessions.append(self)

    async def start(self):
        return {"models": [{"value": "switch-old", "supportedEffortLevels": []}]}

    async def close(self):
        if self.queries:
            events.append({"effect": "native-close-start", "provider_session": self.session_id})
            if case in native_closing_cases and getattr(boundary.app_instance, "_conversation_transition_pending", False):
                hook_entered.set()
                (root / "session-mutation-cleanup-entered").write_text("native cleanup started", encoding="utf-8")
                while not getattr(boundary.app_instance, "_conversation_transition_shutdown_requested", False):
                    await asyncio.sleep(0.005)
            await asyncio.sleep(0)
            if case == "claude-cleanup-failed":
                self.lifecycle.cleanup_errors.append("Controlled outgoing SDK cleanup failure")
            events.append({"effect": "native-close-end", "provider_session": self.session_id})

    async def set_model(self, model):
        assert model == "switch-old"

    async def set_effort(self, value):
        self.effort = value

    async def get_context_usage(self):
        return {"maxTokens": 200000, "totalTokens": 100}

    async def query(self, entry_id, content):
        app = boundary.app_instance
        assert app.backend.session is self and app.backend.name == "claude"
        self.queries += 1
        requests.append({"session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
                         "backend": "claude", "model": app.model_id, "profile": app._active_tool_profile,
                         "tools_enabled": app.tools_enabled, "entry_id": entry_id, "content": content,
                         "provider_session": self.session_id, "resume": self.options.get("resume")})
        events.append({"effect": "native-query", "provider_session": self.session_id, "session_id": app.convo_id})

    async def events(self):
        yield {"type": "assistant", "uuid": "answer-" + str(self.queries), "session_id": self.session_id,
               "message": {"id": "message-" + str(self.queries), "model": "switch-old",
                           "role": "assistant", "content": [{"type": "text", "text": "Controlled native reply"}]}}
        yield {"type": "result", "subtype": "success", "is_error": False, "session_id": self.session_id,
               "result": "Controlled native reply", "usage": {"input_tokens": 10, "output_tokens": 5},
               "total_cost_usd": 0, "num_turns": 1, "duration_ms": 1}

    async def interrupt(self):
        events.append({"effect": "native-interrupt", "provider_session": self.session_id})


class HookInput(io.StringIO):
    def close(self):
        if not self.closed:
            document = json.loads(self.getvalue())
            events.append({"effect": "hook", "document": document})
            if case == "hooks-drift":
                path = root / "hooks.json"
                changed = json.loads(path.read_text(encoding="utf-8"))
                changed["hooks"][0]["argv"] = ["changed-after-review"]
                path.write_text(json.dumps(changed), encoding="utf-8")
        super().close()


class HookProcess:
    """Only the OS child boundary is synthetic; policy and hook runner are real."""
    def __init__(self, argv):
        assert argv == ["session-fixture-hook", "observe"]
        verdict = ""
        if case == "codex-correction":
            previous = [row for row in events if row["effect"] == "hook"]
            verdict = json.dumps({"decision": "allow"} if previous else
                                 {"decision": "deny", "reason": "Controlled corrective followup"})
        self.stdin, self.stdout, self.stderr = HookInput(), io.StringIO(verdict), io.StringIO("")
        self.pid = 987654
        self._litetui_job = None
        self.returncode = None if case in closing_cases else 0
        if case in closing_cases:
            hook_entered.set()
            (root / "session-mutation-hook-entered").write_text("owned hook started", encoding="utf-8")

    def poll(self):
        if case in closing_cases:
            return self.returncode
        # Hook documents are delivered by the real runner's stdin thread.
        delivered = [row["document"] for row in events if row["effect"] == "hook"]
        failed = ("conversation_leave" if case == "hooks-failed-leave" else
                  "conversation_start" if case == "hooks-failed-start" else None)
        return 7 if delivered and delivered[-1]["event"] == failed else 0


def hook_spawn(argv, **_kwargs):
    process = HookProcess(argv)
    hook_processes.append(process)
    return process


def hook_kill(pid, process):
    assert process in hook_processes and pid == process.pid
    events.append({"effect": "hook-kill", "accepted": case != "kill-refused"})
    if case == "kill-refused":
        return False
    process.returncode = -15
    return True


def prepare_hooks():
    if case not in hook_cases:
        return
    from litetui.lifecycle_hooks import Hook, HookConfig
    hook = Hook("session-observer", ("completion_before",) if case == "codex-correction"
                else ("conversation_leave", "conversation_start"), "session-fixture-hook", ("observe",),
                mode="gate" if case == "codex-correction" else "observe")
    (root / "hooks.json").write_text(json.dumps({"version": 1, "hooks": [hook.document()]}), encoding="utf-8")
    assert HookConfig(root / "hooks.json", Path.cwd() / ".litetui/hooks.json").read("global") == (hook,)
    if case != "hooks-approval":
        path = root / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        from litetui import tool_policy
        workspace = Path.cwd().resolve()
        arguments = {"command": json.dumps([hook.executable, *hook.argv]), "cwd": str(workspace), "env": {}}
        settings["tool_always_allow"] = [tool_policy.rule_key(
            hook.approval_name(workspace), tool_policy.SHELL_POLICY.classify(arguments, workspace))]
        path.write_text(json.dumps(settings), encoding="utf-8")


async def serve(app):
    from litetui import gui_rpc, rpc, store_identity, tasks, ttyguard
    from litetui.conversation import ConversationRepository

    boundary.ready = asyncio.Event()
    commands, results = [], []
    pending_apply = None
    original_emit = rpc.rpc_emit
    original_release = app.store.release

    def emit(row):
        if row.get("type") == "response" and row.get("id") in {"session-shutdown", "rust-live-shutdown"}:
            events.append({"effect": "shutdown-ack", "ok": row.get("ok"),
                           "pending": getattr(app, "_conversation_transition_pending", False),
                           "hook_alive": any(process.poll() is None for process in hook_processes),
                           "owned": app.store.owned})
        original_emit(row)

    def release():
        if getattr(app, "_gui_quitting", False):
            events.append({"effect": "lease-release",
                           "pending": getattr(app, "_conversation_transition_pending", False),
                           "hook_alive": any(process.poll() is None for process in hook_processes)})
            assert not getattr(app, "_conversation_transition_pending", False)
            assert not any(process.poll() is None for process in hook_processes)
        return original_release()
    original_open = Path.open

    def open_file(path, mode="r", *args, **kwargs):
        writing = any(flag in mode for flag in ("w", "a", "+", "x"))
        target = apply_target is not None and Path(path) == apply_target / "convo.jsonl"
        if writing and target and case == "write-before":
            events.append({"effect": "write-refused"})
            raise OSError("Controlled transcript write refusal")
        stream = original_open(path, mode, *args, **kwargs)
        return WriteProbe(stream) if writing and target and case == "write-after" else stream

    async def dispatch(command):
        global applied, apply_target, stale_used
        commands.append(copy.deepcopy(command))
        with (root / "session-mutation-commands.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(command) + "\n")
        operation = command["type"]
        if operation.startswith("gui."):
            if operation == "gui.conversations.native_apply":
                if case in {"stale", "one-shot-stale"} and not stale_used:
                    app.settings.max_tokens_chat += 1
                    stale_used = True
                apply_target = app._agent_session.conversation_directory(command["target_session_id"])
            with patch.object(Path, "open", open_file):
                operation_call = gui_rpc.async_dispatch(app, command)
                # Wire owners publish this exact object in their request task.
                result = await operation_call if driver == "rpc" else await asyncio.create_task(operation_call)
            results.append({"command": copy.deepcopy(command), "result": copy.deepcopy(result)})
            with (root / "session-mutation-results.jsonl").open("a", encoding="utf-8") as output:
                output.write(json.dumps(results[-1]) + "\n")
            if operation == "gui.conversations.native_apply":
                applied = result.get("status") in {"renamed", "created"}
                if case == "generic-apply-error":
                    raise RuntimeError("Controlled receipt lost after actual mutation")
            if case == "cancel-review" and operation == "gui.conversations.native_validate" and driver == "rpc":
                await asyncio.sleep(0.3)
            if applied and case == "policy-drift" and operation == "gui.state":
                result["tool_policy"]["tools_enabled"] = not result["tool_policy"]["tools_enabled"]
            if applied and case == "history-drift" and operation in {"gui.conversations.read", "gui.history.native_snapshot"}:
                if "context_sha256" in result:
                    result["context_sha256"] = "0" * 64
                else:
                    result["messages"].append({"role": "assistant", "content": "Contradictory extra history"})
            if applied and case == "title-drift" and operation == "gui.conversations.native_snapshot":
                result["current"]["title"] = "Contradictory title"
            if applied and case == "session-drift" and operation == "gui.state":
                result["session_id"] = "950b27cd-0716-4675-bf5c-f1b234ab30ac"
            return result
        rpc._dispatch(app, command)
        if operation != "shutdown":
            await settle(app)
        return None

    async def prompt(text):
        await dispatch({"type": "prompt", "id": "session-explicit-" + phase + "-" + str(len(pre_requests) + len(requests)),
                        "message": text, "tool_profile": "strict"})

    async def finish_shutdown(*, eof=False):
        if eof:
            rpc._exit_after_engine(app)
        else:
            await dispatch({"type": "shutdown", "id": "session-shutdown"})
        await asyncio.wait_for(asyncio.shield(app._conversation_shutdown_exit_task), 8)

    async def respond_management(command):
        try:
            value = await dispatch(command)
            rpc.publish_management_result(app, command, value)
        except Exception as exc:  # noqa: BLE001 - actual wire isolation
            rpc._respond(command.get("id"), ok=False, error=str(exc))

    with ExitStack() as stack:
        stack.enter_context(patch.object(rpc, "rpc_emit", emit))
        stack.enter_context(patch.object(app.store, "release", release))
        if case in hook_cases:
            stack.enter_context(patch.object(ttyguard, "popen", hook_spawn))
            stack.enter_context(patch.object(ttyguard, "kill_tree", hook_kill))
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, boundary.forbidden))
        try:
            async with asyncio.timeout(60), app.run_test(size=(100, 32)):
                await asyncio.wait_for(boundary.ready.wait(), 15)
                await settle(app)
                ready = next(row for row in boundary.records if row.get("type") == "ready")
                assert ready["launch_status"] == "ready", ready
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "session-hello", "protocol_version": 1})
                if phase == "fresh" and not case.endswith("-pending"):
                    app.conversation = [{"role": "system", "content": "Session fixture system"},
                                        {"role": "user", "content": "History belonging to the original conversation"},
                                        {"role": "assistant", "content": "Retained original answer"}]
                    app._prompt_store_identity = store_identity.capture(app)
                    app._store_injected = True
                    app._materialise_convo()
                    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity,
                                              store_injected=True)
                    await settle(app)
                if case.startswith(("claude-", "codex-")) and phase == "fresh" and driver == "self":
                    await prompt("Explicit old native conversation prompt")
                    assert len(requests) == 1
                    if case.startswith("claude-"):
                        assert app.backend.session is not None
                    pre_requests.extend(requests)
                    requests.clear()
                if case in hook_cases:
                    os.environ["LITETUI_HOOKS"] = "on"
                if case == "task-store":
                    old_task = tasks.Task("fixture-old-task", "fixture", "Completed old task", app.convo_id,
                                          1.0, state=tasks.DONE, ended=2.0, ok=True)
                    app.bg_tasks[old_task.id] = old_task
                    tasks.save([old_task], app.convo_dir, agent_session=app._agent_session)
                busy_worker = None
                if case == "held":
                    from litetui import codex_steering
                    entry = {"id": "held-session-input", "threadId": "old-thread", "turnId": "old-turn",
                             "conversationId": app.convo_id, "state": "queued", "revision": 2,
                             "item": {"content": "NEVER REPLAY HELD SESSION INPUT", "source": "queued",
                                      "tool_profile": "strict"}}
                    app._append({"role": "assistant", "content": "Held before session mutation",
                                 "provider_metadata": {"steering": [entry]}})
                    codex_steering.restore_queue(app)
                    assert len(app._pending_input) == 1 and app._pending_input[0]["_restored_input_held"]
                if case == "active":
                    busy_worker = app.run_worker(asyncio.Event().wait(), name="controlled-busy-work",
                                                 group="fixture", exit_on_error=False)
                    await asyncio.sleep(0)
                before = current(app)
                initial_disk = disk()
                old_path = app.convo_path
                old_bytes = old_path.read_bytes() if old_path.exists() else None
                proof = {}
                transport_before = getattr(app.backend, "_transport", None)
                native_calls_before = copy.deepcopy(codex_calls)
                if driver == "rpc":
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        command = json.loads(line)
                        if case in all_closing_cases and command["type"] == "gui.conversations.native_apply":
                            pending_apply = asyncio.create_task(respond_management(command))
                            continue
                        try:
                            if command["type"].startswith("gui."):
                                await asyncio.create_task(respond_management(command))
                            else:
                                await dispatch(command)
                        except Exception as exc:  # noqa: BLE001 - actual RPC isolation boundary
                            rpc._respond(command.get("id"), ok=False, error=str(exc))
                        if command["type"] == "shutdown":
                            if case in all_closing_cases:
                                exit_task = getattr(app, "_conversation_rewrite_exit_task", None) or app._conversation_shutdown_exit_task
                                await asyncio.wait_for(asyncio.shield(exit_task), 8)
                                assert pending_apply is not None
                                await pending_apply
                            break
                    else:
                        if case in all_closing_cases and pending_apply is not None:
                            await finish_shutdown(eof=True)
                            await pending_apply
                elif phase == "resume":
                    meta, saved = ConversationRepository.read(app.convo_path)
                    proof = {"resumed_meta": meta, "resumed_history": saved}
                    await prompt("Explicit prompt after session restart")
                else:
                    common = {"version": 1, "session_id": app.convo_id,
                              "agent_id": app._agent_session.authority.agent_id,
                              "backend": app.backend.name, "model": app.model_id}
                    snapshot = await dispatch({"type": "gui.conversations.native_snapshot",
                                               "id": "session-snapshot", **common})
                    proof["snapshot"] = snapshot
                    assert disk() == initial_disk and current(app) == before and not requests
                    validate = {"type": "gui.conversations.native_validate", "id": "session-validate",
                                **common, "snapshot": snapshot["snapshot"], "action": action}
                    if action == "rename":
                        validate["title"] = title
                    if case == "foreign":
                        validate["agent_id"] = "3271f610-d4bc-439b-9d23-e24fc6c3e858"
                    if case in {"invalid", "foreign", "held", "active"}:
                        if case == "invalid":
                            validate["title"] = "\n \t"
                        try:
                            await dispatch(validate)
                        except (ValueError, TypeError) as exc:
                            proof["expected_error"] = str(exc)
                        else:
                            raise AssertionError("Invalid review was accepted")
                        assert disk() == initial_disk and current(app) == before
                    else:
                        reviewed = await dispatch(validate)
                        proof["review"] = reviewed
                        assert disk() == initial_disk and current(app) == before and not requests
                        if case not in {"validate-only", "cancel-review"}:
                            fields = ("version", "session_id", "agent_id", "backend", "model",
                                      "snapshot", "review", "action_id", "action", "target_session_id", "title")
                            apply = {"type": "gui.conversations.native_apply", "id": "session-apply",
                                     **{key: reviewed[key] for key in fields}}
                            if case in all_closing_cases:
                                pending_apply = asyncio.create_task(dispatch(apply))
                                assert await asyncio.to_thread(hook_entered.wait, 5)
                                # Actual source prompt admission must refuse during the awaited hook.
                                before_prompt = copy.deepcopy(app.conversation)
                                rpc._dispatch(app, {"type": "prompt", "id": "must-refuse-busy",
                                                    "message": "NEVER SEND DURING SESSION APPLY",
                                                    "tool_profile": "strict"})
                                assert app.conversation == before_prompt and not requests
                                await finish_shutdown(eof=case in {"eof-during-apply", "claude-eof-during-apply"})
                                result = await pending_apply
                            else:
                                result = await dispatch(apply)
                            proof["result"] = result
                            if case in all_closing_cases:
                                assert result["status"] == "failed" and result["close_required"]
                                assert not requests and not app._conversation_transition_pending
                            elif case in {"stale", "one-shot-stale"}:
                                assert result["status"] == "refused" and not result["mutated"]
                                assert app.convo_id == before["session_id"] and disk() == initial_disk
                            elif case in {"write-before", "write-after", "hooks-failed-leave", "hooks-failed-start", "hooks-approval", "hooks-drift", "claude-cleanup-failed"}:
                                assert result["status"] == "failed" and result["close_required"]
                                count = len(requests)
                                await prompt("NEVER SEND AFTER UNCERTAIN SESSION MUTATION")
                                assert len(requests) == count
                            else:
                                expected = "renamed" if action == "rename" else "created"
                                assert result["status"] == expected and not result["close_required"], result
                                assert result["target_session_id"] == reviewed["target_session_id"] == app.convo_id
                                assert result["lifecycle"]["settled"]
                                meta, saved = ConversationRepository.read(app.convo_path)
                                proof.update(meta=meta, history_after_apply=copy.deepcopy(app.conversation),
                                             saved_after_apply=saved, after_apply=current(app))
                                if action == "rename":
                                    assert meta["name"] == reviewed["title"]
                                    assert app.conversation == before["history"]
                                else:
                                    assert app.convo_id != before["session_id"]
                                    assert all(row["role"] == "system" for row in app.conversation)
                                    assert (old_path.read_bytes() if old_path.exists() else None) == old_bytes
                                assert not requests
                                if case.startswith("codex-"):
                                    assert app.backend._transport is transport_before
                                    assert codex_calls == native_calls_before
                                    proof["retained_transport_thread"] = transport_before.thread_id
                                if case in hook_cases and case != "codex-correction":
                                    assert reviewed["effects"]["lifecycle_hooks"] is True
                                    emitted = [row["document"] for row in events if row["effect"] == "hook"]
                                    assert [(row["event"], row["conversation_id"]) for row in emitted] == [
                                        ("conversation_leave", before["session_id"]), ("conversation_start", app.convo_id)]
                                    assert not app._hook_lifecycle and not app._hook_lock.locked()
                                if case == "task-store":
                                    assert "fixture-old-task" in app.bg_tasks
                                    assert not tasks.host_tasks_for_app(app)
                                    fresh = tasks.Task("fixture-new-task", "fixture", "Completed new task", app.convo_id,
                                                       3.0, state=tasks.DONE, ended=4.0, ok=True)
                                    app.bg_tasks[fresh.id] = fresh
                                    tasks.save_by_convo(app.bg_tasks.values(), agent_session=app._agent_session)
                                    proof["task_rows"] = {str(path): json.loads(path.read_text(encoding="utf-8"))
                                                          for path in (old_path.parent / tasks.STORE, app.convo_dir / tasks.STORE)}
                                if case == "replay":
                                    try:
                                        replayed = await dispatch(apply)
                                    except (ValueError, TypeError) as exc:
                                        proof["replay_error"] = str(exc)
                                    else:
                                        assert replayed["status"] == "refused" and not replayed["mutated"]
                                await prompt("Explicit prompt after verified session change")
                                assert len(requests) == (2 if case == "codex-correction" else 1)
                                assert all(row["session_id"] == app.convo_id for row in requests)
                if busy_worker is not None:
                    busy_worker.cancel()
                await settle(app)
                if case.startswith("claude-"):
                    from litetui.claude_backend import close_native, settle_close
                    close_native(app, app.backend)
                    if case != "claude-cleanup-failed":
                        assert not await settle_close(app)
                    else:
                        await settle_close(app)
                if case in hook_cases:
                    proof["hook_results"] = {str(key): {"allowed": row.allowed, "reason": row.reason}
                                             for key, row in app.hook_results.items()}
                    assert not getattr(app, "_tool_approval_pending", None)
                    assert not any(row.get("type") == "tool_approval" for row in boundary.records)
                    os.environ["LITETUI_HOOKS"] = "off"
                after = current(app)
                final_disk = disk()
                old_unchanged = (old_path.read_bytes() if old_path.exists() else None) == old_bytes
        except RuntimeError as exc:
            if case not in {"claude-cleanup-failed", "kill-refused"} or "unproven process cleanup" not in str(exc):
                raise
            assert app.store.owned and not getattr(app, "_conversation_transition_pending", False)
            proof["shutdown_refused"] = str(exc)
        finally:
            await app.client.close()
        import litetui.app as imported_app
        assert Path(imported_app.__file__).resolve() == (source / "src/litetui/app.py").resolve()
        report = {"before": before, "after": after, "proof": proof,
                  "initial_disk": initial_disk, "final_disk": final_disk, "old_unchanged": old_unchanged,
                  "requests": requests, "pre_requests": pre_requests, "events": events, "commands": commands, "results": results,
                  "calls": boundary.calls, "denied": boundary.denied,
                  "source_app": str(Path(imported_app.__file__).resolve()),
                  "shutdown": getattr(app, "_conversation_shutdown_report", None),
                  "store_owned_after_teardown": app.store.owned,
                  "pending_after_teardown": getattr(app, "_conversation_transition_pending", False),
                  "hook_process_codes": [process.poll() for process in hook_processes],
                  "codex_calls": codex_calls,
                  "wire": [row for row in boundary.records if row.get("type") == "gui.event"]}
        (root / ("session-mutation-" + phase + "-report.json")).write_text(json.dumps(report), encoding="utf-8")


def main():
    if case.startswith("claude-"):
        # Load SDK type declarations while retaining Popen's generic type.
        # Construction is still tripwired and the real SDK is never started.
        with patch.object(subprocess.Popen, "__init__", boundary.forbidden), \
                patch.object(socket.socket, "connect", boundary.forbidden):
            __import__("claude_agent_sdk")
    prepare_hooks()
    with ExitStack() as stack:
        if case.startswith("codex-"):
            from litetui.codex_app_server import AppServer
            stack.enter_context(patch.object(AppServer, "start", codex_start))
            stack.enter_context(patch.object(AppServer, "request", codex_request))
            stack.enter_context(patch.object(AppServer, "close", codex_close))
        stack.enter_context(patch.object(boundary, "serve", serve))
        stack.enter_context(patch.object(boundary, "inference", inference))
        stack.enter_context(patch.object(boundary, "ControlledSession",
                                        NativeSession if case.startswith("claude-") else boundary.ControlledSession))
        boundary.main()


if __name__ == "__main__":
    main()
