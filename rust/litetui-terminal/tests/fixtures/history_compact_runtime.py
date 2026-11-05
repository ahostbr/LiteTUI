"""Actual owned manual-compaction RPC with controlled external boundaries."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

spec = importlib.util.spec_from_file_location(
    "_compact_history_support", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
history.report["sdk_requests"] = []

CASE = os.environ.get("RUST_COMPACT_CASE", "compact")
DRIVER = os.environ.get("RUST_COMPACT_DRIVER", "self")
SNAPSHOT = "gui.conversations.native_compact_snapshot"
VALIDATE = "gui.conversations.native_compact_validate"
APPLY = "gui.conversations.native_compact_apply"
SETTLED = "gui.conversations.native_compact_settled"
HINT = " Preserve the exact tool result 雪🛰️e\u0301\r\nand the unfinished task.  "
SUMMARY = "Controlled compacted continuity summary"
WAKE_ANSWER = "Controlled answer after the genuine compaction wake"
SYSTEM = "COMPACT SYSTEM: preserve this exact instruction and Unicode 雪."
MEMORY = "CURRENT MEMORY: independently read from the owned memory file."
STALE_MEMORY = "STALE SNAPSHOT: must be removed from rebuilt context."
WRITE_CONTENT = "A real compaction tool wrote this disposable artifact.\n"
QUESTION_ARGS = {"questions": [{"label": "Compact choice", "question": "How should the task continue?",
    "options": [{"title": "Preserve the task", "description": "Use the exact retained context"},
                {"title": "Wait", "description": "Do not continue"}], "multiSelect": False}]}
QUESTION_NOTE = "Explicit controlled compaction answer 雪"
QUESTION_RESULT = ("[ask_user_question] SUBMITTED — 1 of 1 answered\n\n"
    "  Compact choice — ANSWERED\n    [x] Preserve the task  (Use the exact retained context)\n"
    "    [ ] Wait  (Do not continue)\n"
    "    note: " + QUESTION_NOTE + "\n\n"
    "These selections and notes are the user's answers — proceed on that basis.")
stream_release: asyncio.Event | None = None
streams: list[Any] = []
question_threads: list[dict] = []
workers: list[Any] = []


def stable(app):
    return {"identity": history.identity(app), "disk": history.disk(app),
        "settings": asdict(app.settings), "authority": asdict(app._agent_session.authority),
        "handles": [id(value) for value in (app.store, app.settings, app.backend, app.client,
                    app._agent_session, app._launch_options, app._pending_input)],
        "store_identity": copy.deepcopy(app._prompt_store_identity),
        "store_injected": app._store_injected}


def owned_transcript(app):
    path = app.convo_path.resolve()
    assert path.is_relative_to(history.root.resolve())
    assert path.name == "convo.jsonl" and path.parent.name == app.convo_id
    return path


def records(app):
    result = []
    for line in owned_transcript(app).read_bytes().splitlines():
        try:
            result.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            pass
    return result


def seed(app):
    from litetui import appsvc, paths, store_identity, tool_policy

    memory = app._agent_session.memory_root
    assert memory.resolve().is_relative_to(history.root.resolve())
    for name, body in (("memory.md", MEMORY), ("soul.md", ""), ("handoff.md", "")):
        (memory / name).write_text(body, encoding="utf-8")
    stale = appsvc.STORE_OPENING + "### memory.md (current contents)\n\n" + STALE_MEMORY + "\n"
    system = {"role": "system", "content": SYSTEM + stale}
    head = [{"role": "user", "content": "OLDER USER: summarize this request"},
        {"role": "assistant", "content": "OLDER ANSWER: retain its meaning"},
        {"role": "user", "content": "TAIL REQUEST: preserve the complete tool pair"}]
    tail = [{"role": "assistant", "content": "TAIL CALL", "tool_calls": [
        {"id": "compact-preserved-tool", "type": "function",
         "function": {"name": "fixture_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "compact-preserved-tool", "content": "TAIL TOOL RESULT 雪"},
        {"role": "assistant", "content": "TAIL FINAL ANSWER"}]
    app.conversation = [system, *head, *tail]
    if CASE == "too-short":
        app.conversation = [system, *head[:2]]
    elif CASE == "no-system":
        app.conversation = [*head, *tail]
    elif CASE == "already-recent":
        app.settings.compact_keep_recent = 100
    elif CASE == "keep-zero":
        app.settings.compact_keep_recent = 0
    app._prompt_store_identity = None if CASE == "no-system" else store_identity.capture(app)
    app._store_injected = None if CASE == "no-system" else True
    if CASE.startswith("codex-native"):
        from litetui.model_transport import for_app

        transport = for_app(app)
        server = transport.server
        assert server.process is not None and server.initialized
        server.session.ready(server.session.begin())
        transport.thread_id = "history-native-thread"
        transport.process = server.process
        app.conversation[-1]["provider_metadata"] = {
            "provider": "codex", "app_server_thread_id": transport.thread_id, "app_server_turn_id": "history-native-turn"}
    app._materialise_convo()
    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=app._store_injected)
    assert not app.store.persist_error
    artifact = Path.cwd() / "compact-owned-output.txt"
    assert artifact.resolve().parent == Path.cwd().resolve()
    write_args = {"path": str(artifact), "content": WRITE_CONTENT}
    if CASE.startswith("owner-verified"):
        artifact = app._owner_memory_dir / "NOW.md"
        write_args = {"path": str(artifact), "content": "Fresh private owner handoff.\n"}
        live_files = appsvc.store_block(app, live=True)
        history.report["owner"] = {"id": app._agent_session.authority.agent_id,
            "name": app._agent_session.authority.name, "privileged": bool(app._owner_seat),
            "store": store_identity.capture(app), "session": app.convo_id}
    if CASE in {"tools-on", "tool-then-empty", "tool-cap"} or CASE.startswith("owner-verified"):
        app.settings.tool_always_allow = [tool_policy.rule_key(
            "write", tool_policy.WRITE_POLICY.classify(write_args, paths.ROOT))]
    if CASE == "held":
        from litetui import codex_steering
        entry = {"id": "compact-held", "threadId": "old-thread", "turnId": "old-turn",
            "conversationId": app.convo_id, "state": "queued", "revision": 1,
            "item": {"content": "HELD INPUT NEVER REPLAY", "source": "queued", "tool_profile": "strict"}}
        app._append({"role": "assistant", "content": "HELD DELIVERY LEDGER",
            "provider_metadata": {"steering": [entry]}})
        codex_steering.restore_queue(app)
        assert app._pending_input and app._pending_input[0]["_restored_input_held"]
    original = copy.deepcopy(app.conversation)
    actual_tail = [] if CASE == "keep-zero" else tail
    if CASE == "already-recent" or CASE == "too-short":
        actual_tail = original[1:]
    actual_head = original[(1 if original[0]["role"] == "system" else 0):len(original) - len(actual_tail)] if actual_tail else original[1:]
    live_store = ("\n\n## Your store, as it stands right now\n\nRe-read from disk just now.\n\n"
        "### memory.md (current contents)\n\n" + MEMORY + "\n")
    history.report.update(original=original, expected_head=copy.deepcopy(actual_head),
        expected_tail=copy.deepcopy(actual_tail), live_store=live_files if CASE.startswith("owner-verified") else live_store,
        before=stable(app), transcript_path=str(app.convo_path), records_before=records(app),
        archive_before=owned_transcript(app).read_bytes().hex(), artifact=str(artifact),
        write_args=write_args)


CLAUDE_NATIVE = "12345678-1111-4111-8111-123456789abc"
CLAUDE_TERMINAL = "22345678-1111-4111-8111-123456789abc"
CLAUDE_WAKE_SESSION = "32345678-1111-4111-8111-123456789abc"
CLAUDE_WAKE_RESULT = "42345678-1111-4111-8111-123456789abc"
# The summary turn (STEP 1) writes the store file, or the wake turn does; both through the native tool.
CLAUDE_SUMMARY_TOOL = CASE in {"claude-segment-tools", "claude-segment-approval", "claude-segment-approval-denied",
                               "claude-segment-stop-approval"}
CLAUDE_WAKE_TOOL = CASE == "claude-segment-wake-approval"
CLAUDE_TOOLS = CLAUDE_SUMMARY_TOOL or CLAUDE_WAKE_TOOL
CLAUDE_WAKE = CASE in {"claude-segment-wake", "claude-segment-wake-approval"}


def claude_client():
    """The controlled SDK client: scripted frames, and the PreToolUse hook the SDK would run."""
    from claude_agent_sdk.types import ResultMessage

    class Client:
        def __init__(self, options):
            self.options = options
            self.frames = asyncio.Queue()

        async def connect(self):
            pass

        async def get_server_info(self):
            return {}

        async def get_context_usage(self):
            return {"maxTokens": 200000, "totalTokens": 10}

        async def set_model(self, model):
            assert model == history.boundary.app_instance.model_id

        async def tool(self, session_id, name, args, tool_id):
            """Assistant tool_use, then the real hook (on THIS owner task, as the SDK does), then the CLI's effect."""
            await self.frames.put({"type": "assistant", "uuid": CLAUDE_TERMINAL, "session_id": session_id,
                "message": {"id": "msg-" + tool_id, "model": "claude-fixture", "usage": None, "stop_reason": "tool_use",
                    "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": args}]}})
            matcher = self.options.hooks["PreToolUse"][0]
            verdict = (await matcher.hooks[0]({"tool_name": name, "tool_input": args}, tool_id, None))["hookSpecificOutput"]
            allowed = verdict["permissionDecision"] == "allow"
            if allowed:
                Path(args["file_path"]).write_text(args["content"], encoding="utf-8")
            history.report.setdefault("claude_tool_verdicts", []).append(verdict)
            await self.frames.put({"type": "user", "uuid": CLAUDE_TERMINAL, "message": {"content": [{
                "type": "tool_result", "tool_use_id": tool_id, "is_error": not allowed,
                "content": "wrote the store file" if allowed else verdict.get("permissionDecisionReason", "denied")}]}})

        async def query(self, prompt):
            from litetui.app import WAKE_AFTER_COMPACT

            bootstrap = prompt == "fixture initial turn"
            wake = prompt == WAKE_AFTER_COMPACT
            args = {"file_path": history.report["artifact"], "content": WRITE_CONTENT}
            if bootstrap:
                session_id, result_id, result = CLAUDE_NATIVE, CLAUDE_NATIVE, "Hello"
            elif wake:
                history.report.setdefault("claude_wake_prompts", []).append(prompt)
                history.report["claude_verifications_before_wake"] = history.report.get("claude_verifications", 0)
                session_id, result_id, result = CLAUDE_WAKE_SESSION, CLAUDE_WAKE_RESULT, WAKE_ANSWER
                if CLAUDE_WAKE_TOOL:
                    await self.tool(session_id, "Write", args, "toolu_wake_write")
                await self.frames.put({"type": "assistant", "uuid": CLAUDE_WAKE_RESULT, "session_id": session_id,
                    "message": {"id": "msg-wake-text", "model": "claude-fixture", "usage": None, "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": WAKE_ANSWER}]}})
            else:
                history.report.setdefault("claude_summary_prompts", []).append(prompt)
                assert history.explicit
                session_id = CLAUDE_TERMINAL if CASE == "claude-segment-foreign" else CLAUDE_NATIVE
                result_id, result = CLAUDE_TERMINAL, SUMMARY
                if CLAUDE_SUMMARY_TOOL:
                    await self.tool(session_id, "Write", args, "toolu_store_write")
            await self.frames.put(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                num_turns=1, session_id=session_id, uuid=result_id, terminal_reason="completed", result=result))

        async def receive_messages(self):
            while True:
                yield await self.frames.get()

        async def disconnect(self):
            if CASE == "claude-segment-close":
                raise RuntimeError("controlled SDK close failure")

    return Client


async def seed_claude(app):
    from litetui import tool_policy
    from litetui.claude_events import ClaudeEventStream
    from litetui.claude_session import ClaudeSession
    from litetui.claude_tools import ClaudeTools
    from litetui.claude_turn import launch_workspace, ledger_for

    native, terminal = CLAUDE_NATIVE, CLAUDE_TERMINAL
    Client = claude_client()
    app.tools_enabled = CLAUDE_TOOLS
    ledger = ledger_for(app)
    segment = ledger.select_segment(launch_workspace(app))
    bridge = ClaudeTools(app, app.backend, segment["id"], workspace=segment["workspace"])
    if CLAUDE_TOOLS:
        # The options a tools-on session really opens with: native tool names, hooks, permission mode.
        options = NS(effort=None, cwd=segment["workspace"], strict_mcp_config=True, **bridge.sdk_options())
        if CASE == "claude-segment-tools":
            # A standing allow rule, so the native Write needs no prompt (the other cases prompt over RPC).
            write = {"file_path": history.report["artifact"], "content": WRITE_CONTENT, "path": history.report["artifact"]}
            app.settings.tool_always_allow = [tool_policy.rule_key(
                "Write", tool_policy.WRITE_POLICY.classify(write, Path(segment["workspace"])))]
    else:
        options = NS(tools=[], mcp_servers={}, strict_mcp_config=True, effort=None, cwd=segment["workspace"])
    session = ClaudeSession(options, client_factory=Client)
    app.backend.session, app.backend.segment_id = session, segment["id"]
    await session.start()
    await session.query("fixture-bootstrap", "fixture initial turn")
    async for _ in session.events():
        pass
    ledger.bind_session(segment["id"], session.session_id)
    app.backend._claude_tools = bridge
    app.backend._claude_events = ClaudeEventStream(session_id=native)
    history.report.update(old_ledger=ledger.document, claude_session=native, claude_result=terminal)
    if CASE == "claude-segment-ledger":
        original = ledger._save

        def fail_save():
            if ledger.selected["id"] != segment["id"]:
                raise OSError("controlled new segment write failure")
            return original()

        ledger._save = fail_save
    if CLAUDE_WAKE:
        app.settings.wake_after_compact = True


def mark(stage, **extra):
    current = history.report.get("review", {})
    payload = {"stage": stage, "action_id": current.get("action_id"),
        "operation_id": history.report.get("apply_operation"),
        "root_execution_id": current.get("root_execution_id"), **extra}
    (history.root / "compact-stage.json").write_text(json.dumps(payload), encoding="utf-8")
    history.report.setdefault("stages", []).append(payload)


async def until(predicate, timeout=20):
    async with asyncio.timeout(timeout):
        while not (result := predicate()):
            await asyncio.sleep(0.01)
        return result


def delta(content=None, tools=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=tools),
        finish_reason=finish)], usage=None)


class ControlledStream:
    def __init__(self, kind, request):
        self.kind, self.request = kind, copy.deepcopy(request)
        self.closed = False
        streams.append(self)
        history.boundary.streams.append(self)

    def __aiter__(self):
        async def chunks():
            from litetui.app import WAKE_AFTER_COMPACT

            if CASE in {"stop-stream", "quit-stream", "eof-stream"} and self.kind == "compact":
                mark("root-maintenance")
                assert stream_release is not None
                await stream_release.wait()
            messages = self.request["messages"]
            is_followup = messages[-1].get("role") == "tool"
            on_wake = self.kind == "wake"
            asks = "question" in CASE and (("wake" in CASE) == on_wake)
            writes = CASE.startswith("owner-verified") or CASE in {"tools-on", "tools-off", "tool-then-empty", "tool-cap"} or ("approval" in CASE and (("wake" in CASE) == on_wake))
            if (asks or writes) and (not is_followup or CASE == "tool-cap"):
                name = "ask_user_question" if asks else "write"
                args = QUESTION_ARGS if asks else history.report["write_args"]
                call_id = f"compact-live-call-{len(history.report['sdk_requests'])}"
                call = NS(index=0, id=call_id, function=NS(name=name, arguments=json.dumps(args)))
                history.report.setdefault("provider_tool_calls", []).append(
                    {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}})
                yield delta(tools=[call])
                yield delta(finish="tool_calls")
            else:
                text = "" if CASE in {"empty-summary", "tool-then-empty"} and not on_wake else WAKE_ANSWER if on_wake else SUMMARY
                if text:
                    yield delta(content=text)
                yield delta(finish="stop")
            assert not messages or messages[-1].get("content") != WAKE_AFTER_COMPACT or on_wake
        return chunks()

    async def close(self):
        try:
            if CASE == "stop-close" and self.kind == "compact":
                mark("stream-close")
                assert stream_release is not None
                await stream_release.wait()
        finally:
            self.closed = True


async def completions(resource, **request):
    if not history.explicit:
        history.boundary.forbidden()
    from litetui.app import COMPACT_PROMPT, WAKE_AFTER_COMPACT

    app = history.boundary.app_instance
    assert resource._client is app.client
    assert request["model"] == app._agent_session.authority.model
    assert "origin" not in request and "purpose" not in request
    messages = request["messages"]
    if not request.get("stream"):
        kind = "card-summary"
    elif any(row.get("role") == "user" and row.get("content") == WAKE_AFTER_COMPACT for row in messages):
        kind = "wake"
    else:
        kind = "compact"
    history.report.setdefault("sdk_requests", []).append({"kind": kind, "request": copy.deepcopy(request),
        "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
        "backend": app.backend.name, "model": app.model_id})
    if kind == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled card title"))])
    if kind == "compact" and not any(row.get("role") == "tool" and str(row.get("tool_call_id", "")).startswith("compact-live-call-") for row in messages):
        expected = copy.deepcopy(history.report["expected_head"])
        prompt = COMPACT_PROMPT
        if CASE.startswith("owner-verified"):
            from litetui import owner_mode
            prompt = owner_mode.compact_prompt(app._owner_memory_dir)
        expected.append({"role": "user", "content": prompt + ("\n\n" + history.report["hint"] if history.report["hint"] else "")})
        original = history.report["original"]
        if original[0]["role"] == "system":
            expected.insert(0, {**original[0], "content": original[0]["content"] + history.report["live_store"]})
        assert messages == expected, {"actual": messages, "expected": expected}
        history.report["expected_compaction_request"] = expected
    if kind == "wake" and not any(row.get("role") == "tool" and str(row.get("tool_call_id", "")).startswith("compact-live-call-") for row in messages):
        from litetui.appsvc import STORE_OPENING
        expected = [{"role": "system", "content": SYSTEM + STORE_OPENING
            + "### memory.md (current contents)\n\n" + MEMORY + "\n"},
            {"role": "user", "content": "[Summary of earlier conversation, which has been compacted away]\n\n" + SUMMARY},
            {"role": "assistant", "content": "Understood — I have that context."},
            *copy.deepcopy(history.report["expected_tail"]),
            {"role": "user", "content": WAKE_AFTER_COMPACT}]
        assert messages == expected, {"actual": messages, "expected": expected}
        history.report["expected_wake_request"] = expected
    if "question" in CASE and messages[-1].get("role") == "tool" and str(messages[-1].get("tool_call_id", "")).startswith("compact-live-call-"):
        first = history.report["expected_wake_request" if kind == "wake" else "expected_compaction_request"]
        question_call = history.report["provider_tool_calls"][-1]
        expected = [*copy.deepcopy(first), {"role": "assistant", "content": None, "tool_calls": [question_call]},
            {"role": "tool", "tool_call_id": question_call["id"], "name": "ask_user_question", "content": QUESTION_RESULT}]
        assert messages == expected, "The complete followup must contain only the exact pending question and submitted answer"
        history.report["expected_question_followup"] = expected
    if CASE == "late-readonly" and kind == "compact":
        import stat
        owned_transcript(app).chmod(stat.S_IREAD)
        history.report["late_readonly_injected"] = True
    if CASE == "provider-failure" and kind == "compact":
        raise RuntimeError("Controlled compaction provider failure")
    return ControlledStream(kind, request)


async def serve(app):
    import socket
    import stat
    import threading
    from contextlib import ExitStack
    from unittest.mock import patch

    from litetui import ask_user_question, rpc, ttyguard
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    global stream_release
    history.boundary.ready = asyncio.Event()
    stream_release = asyncio.Event()
    exited = asyncio.Event()
    reader = None
    serial = 0
    seeded = False
    readonly = None
    original_exit, original_release = app.exit, app.store.release
    original_dispatch, original_worker, original_emit = rpc._dispatch, LiteTUI.run_worker, rpc.rpc_emit
    original_question = ask_user_question.run
    original_write = app.store.write_record
    original_ready = app._ensure_chat_ready
    original_clear = app._clear_screen
    original_command = ttyguard.run

    def attempt():
        return (getattr(app, "_conversation_action_attempt", None)
            or getattr(app, "_conversation_compact_attempt", None)
            or getattr(app, "_conversation_rewrite_attempt", None))

    def tool_state():
        current = attempt()
        calls = getattr(current, "tool_calls", None) or ()
        return [{"done": call.future is None or call.future.done(),
            "producer_done": call.producer is None or call.producer.done(),
            "callback_retired": call.future is None or call.retired.is_set(),
            "lifetime_cancelled": call.cancelled.is_set(), "origin": asdict(call.origin)}
            for call in calls]

    def collect():
        if not seeded:
            return
        task = getattr(app, "_conversation_compact_shutdown_task", None)
        if task is not None and task.done() and not task.cancelled():
            history.report["shutdown"] = copy.deepcopy(task.result())
        raw = owned_transcript(app).read_bytes()
        history.report.update(after=stable(app), live=copy.deepcopy(app.conversation),
            durable=ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1],
            persist_error=app.store.persist_error, records_after=records(app),
            archive_prefix_preserved=raw.startswith(bytes.fromhex(history.report["archive_before"])),
            question_threads=copy.deepcopy(question_threads),
            question_pending=list(getattr(app, "_rpc_pending_asks", {})),
            tool_futures=tool_state(), approval_pending=list(getattr(app, "_approval_waiters", {})),
            approval_authorities=list(getattr(app, "_approval_authority_records", {})),
            artifact_exists=Path(history.report["artifact"]).exists())
        if CASE.startswith("claude-segment"):
            from litetui import restored_claude
            history.report["final_ledger"] = restored_claude._read(app.store).document
        if history.report["artifact_exists"]:
            history.report["artifact_text"] = Path(history.report["artifact"]).read_text(encoding="utf-8")

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return original_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            active = [w.name for w in workers if not w.is_finished and w.group == "chat"]
            calls = tool_state()
            row = {"effect": "compact-lease-release", "active_chat": active,
                "pending_questions": list(getattr(app, "_rpc_pending_asks", {})),
                "pending_approvals": list(getattr(app, "_approval_waiters", {})),
                "tools": calls, "threads_retired": all(t["retired"] for t in question_threads)}
            history.events.append(row)
            assert not active and not row["pending_questions"] and not row["pending_approvals"]
            assert row["threads_retired"]
            assert all(c["done"] and c["producer_done"] and c["callback_retired"] for c in calls)
        return original_release()

    def command(argv, *args, **kwargs):
        if CASE.startswith("owner-verified") and len(argv) > 2 and argv[1] == "--no-optional-locks":
            return history.boundary.real_git_read(argv, *args, **kwargs)
        artifact = Path(history.report["artifact"]) if seeded else None
        if artifact is not None and argv == ["git", "check-attr", "-z", "eol", "--", artifact.name]:
            assert not args and kwargs == {"cwd": artifact.parent, "timeout": 2}
            history.report.setdefault("line_ending_lookups", []).append({"argv": argv, "cwd": str(artifact.parent)})
            return NS(returncode=1, stdout="", stderr="Controlled disposable non-repository workspace")
        return original_command(argv, *args, **kwargs)

    def clear_screen(*args, **kwargs):
        if seeded:
            history.report.setdefault("clear_screen_calls", []).append(copy.deepcopy(kwargs))
        return original_clear(*args, **kwargs)

    def launched(self, *args, **kwargs):
        worker = original_worker(self, *args, **kwargs)
        workers.append(worker)
        return worker

    def question(args, owner):
        assert owner is app and args == QUESTION_ARGS
        assert threading.current_thread() is not threading.main_thread()
        from litetui import conversation_rewrite
        origin = conversation_rewrite.event_origin(app)
        assert origin is not None and origin.execution_id is not None
        assert not conversation_rewrite._authorized(app)
        try:
            conversation_rewrite.require_open(app)
        except ValueError:
            pass
        else:
            raise AssertionError("Event-only question thread acquired execution admission")
        row = {"thread_id": threading.get_ident(), "retired": False, "origin": asdict(origin)}
        question_threads.append(row)
        try:
            row["result"] = original_question(args, owner)
            return row["result"]
        finally:
            row["retired"] = True

    def dispatch(self, command):
        history.commands.append(copy.deepcopy(command))
        if command["type"] == APPLY:
            history.explicit = True
            history.report["apply_operation"] = command["id"]
            history.report["before_apply"] = stable(app)
        return original_dispatch(self, command)

    def emit(row):
        command = next((c for c in history.commands if c.get("id") == row.get("id")), None)
        if row.get("type") == "response":
            wire = len(json.dumps(row).encode("utf-8"))
            history.observations.append({"type": command["type"] if command else None, "wire_bytes": wire})
            assert wire <= history.MAX_WIRE
            history.report.setdefault("responses", []).append(copy.deepcopy(row))
            if command and row.get("ok"):
                field = {SNAPSHOT: "snapshot", VALIDATE: "review", APPLY: "apply", SETTLED: "settled"}.get(command["type"])
                if field:
                    history.report[field] = copy.deepcopy(row["result"])
                    if field == "review":
                        history.report["hint"] = row["result"]["hint"]
                if command["type"] == "shutdown":
                    history.report["shutdown_response"] = copy.deepcopy(row)
                    history.report["shutdown"] = copy.deepcopy(row["result"].get("conversation_compact_shutdown"))
            if command and command["type"] == "abort" and row.get("ok") and row["result"]["stopped"]:
                assert stream_release is not None
                stream_release.set()  # Soft Stop is observed at the next controlled chunk/readiness boundary.
                history.report["stop_response"] = copy.deepcopy(row)
        if row.get("type") == "gui.event":
            inner = row["event"]
            if inner.get("type") in {"user_input_requested", "tool_approval_requested"}:
                history.report["input_request"] = copy.deepcopy(row)
                mark("question" if inner["type"] == "user_input_requested" else "approval",
                    request_id=inner["id"], execution_id=row["execution_id"])
            if inner.get("type") == "turn_start" and inner.get("compact_segment"):
                mark("wake", execution_id=row["execution_id"])
        return original_emit(row)

    async def call(operation, *, check=True, **fields):
        nonlocal serial
        serial += 1
        command = {"type": operation, "id": f"compact-{serial}", **fields}
        rpc._dispatch(app, command)
        response = await until(lambda: next((r for r in history.boundary.records
            if r.get("type") == "response" and r.get("id") == command["id"]), None), 40)
        if check:
            assert response["ok"], response
        return response

    def reader_start():
        nonlocal reader
        assert reader is None
        reader = threading.Thread(target=rpc._reader_loop, args=(app,), name="compact-real-rpc-stdin", daemon=True)
        reader.start()

    async def shutdown(eof=False):
        if eof:
            reader_start()
            mark(history.report.get("stages", [{}])[-1].get("stage", "shutdown"))
            await asyncio.wait_for(exited.wait(), 20)
        else:
            await call("shutdown")
            await asyncio.wait_for(exited.wait(), 20)

    def faulty_write(record):
        if history.explicit:
            history.report.setdefault("owned_write_attempts", []).append(record.get("type"))
        if CASE == "claude-segment-transcript" and record.get("type") == "truncate":
            return None
        fault = CASE.removeprefix("persist-")
        eligible = record.get("type") == ("edit" if fault in {"system-edit", "receipts"} else "truncate")
        if not CASE.startswith("persist-") or not eligible:
            return original_write(record)
        history.report["publication_attempts"] = history.report.get("publication_attempts", 0) + 1
        if fault in {"silent", "system-edit"}:
            return None
        if fault == "torn":
            with owned_transcript(app).open("ab") as handle:
                handle.write(b'{"type":"truncate","incomplete":')
            return None
        changed = copy.deepcopy(record)
        if fault == "wrong":
            changed["prepend"][0]["content"] = "WRONG UNREVIEWED COMPACTION"
        elif fault == "receipts":
            changed["store_injected"] = not changed.get("store_injected", False)
        else:
            raise AssertionError(f"Unknown persistence injection: {fault}")
        return original_write(changed)

    async def ready(*args, **kwargs):
        if history.explicit and CASE in {"readiness-failure", "stop-readiness", "quit-readiness", "eof-readiness"}:
            if CASE == "readiness-failure":
                from litetui.llm_backend import BackendError
                raise BackendError("Controlled readiness refusal")
            mark("readiness")
            assert stream_release is not None
            await stream_release.wait()
        return await original_ready(*args, **kwargs)

    async def self_driver():
        common = {"version": 1, "session_id": app.convo_id,
            "agent_id": app._agent_session.authority.agent_id, "backend": app.backend.name, "model": app.model_id}
        descriptor = (await call(history.SNAPSHOT, **history.common(app)))["result"]
        selection = {"history_snapshot": descriptor["snapshot"], "context_sha256": descriptor["context_sha256"]}
        if CASE.startswith("legacy-"):
            variants = {
                "legacy-gui-compact": ("gui.conversations.compact", {"session_id": app.convo_id}),
                "legacy-prompt": ("prompt", {"message": "/compact literal hint"}),
                "legacy-prompt-case": ("prompt", {"message": "  /COMPACT literal hint  "}),
                "legacy-run-command": ("run_command", {"line": "/compact literal hint"}),
                "legacy-run-command-space": ("run_command", {"line": " \t/COMPACT\r\n literal hint  "}),
                "legacy-gui-prompt": ("gui.prompt.submit", {"message": "/compact literal hint"}),
            }
            operation, fields = variants[CASE]
            history.report["legacy_before"] = stable(app)
            history.report["legacy_response"] = await call(operation, check=False, **fields)
            await history.settle(app)
            history.report["legacy_after"] = stable(app)
            return
        if CASE == "foreign-session":
            common["session_id"] = "42345678-1234-4123-8123-123456789abc"
        if CASE == "stale-selection":
            selection["context_sha256"] = "0" * 64
        snap_response = await call(SNAPSHOT, check=False, **common, **selection)
        history.report["snapshot_response"] = snap_response
        if not snap_response["ok"]:
            history.report["refused_at"] = "snapshot"
            return
        snapshot = snap_response["result"]
        if not snapshot["eligible"]:
            history.report["refused_at"] = "eligibility"
            return
        hint = "" if CASE == "empty-hint" or CASE.startswith("codex-native") else " " if CASE == "whitespace-hint" else HINT
        if CASE == "hint-boundary":
            hint = "é" * 32768
        elif CASE == "hint-over-limit":
            hint = "é" * 32768 + "x"
        elif CASE == "hint-nul":
            hint = "forbidden\0hint"
        history.report["hint"] = hint
        response = await call(VALIDATE, check=False, **common, **selection, snapshot=snapshot["snapshot"], hint=hint)
        history.report["validate_response"] = response
        if not response["ok"]:
            history.report["refused_at"] = "validate"
            return
        review = response["result"]
        payload = {k: copy.deepcopy(v) for k, v in review.items() if k != "kind"}
        if CASE == "stale-settings":
            app.settings.compact_max_tokens += 1
        elif CASE == "claude-segment-drift":
            app.backend.session.lifecycle.generation += 1
        elif CASE == "codex-native-drift":
            app.backend.app_server.session.ready(app.backend.app_server.session.begin())
        elif CASE == "stale-history":
            app._append({"role": "assistant", "content": "Coherent change after the review"})
        elif CASE == "stale-policy":
            app.settings.tool_deny = ["write:workspace_write"]
        elif CASE == "wrong-root":
            payload["root_execution_id"] = "52345678-1234-4123-8123-123456789abc"
        elif CASE == "bool-version":
            payload["version"] = True
        task = asyncio.create_task(call(APPLY, check=False, **payload))
        if CASE.startswith(("stop-", "quit-", "eof-")) or CASE in {"question", "question-wake", "approval", "approval-wake", "approval-denied",
                "claude-segment-approval", "claude-segment-wake-approval", "claude-segment-approval-denied",
                "claude-segment-stop-approval"}:
            await until(lambda: history.report.get("input_request") if "question" in CASE or "approval" in CASE
                else history.report.get("stages"))
            if CASE.startswith("stop-") or CASE == "claude-segment-stop-approval":
                history.report["stop_response"] = await call("abort")
            elif CASE.startswith(("quit-", "eof-")):
                await shutdown(eof=CASE.startswith("eof-"))
                response = await task
                history.report["apply_response"] = response
                return
            else:
                request = history.report["input_request"]["event"]
                if "question" in CASE:
                    history.report["answer_response"] = await call("answer", ask_id=request["id"],
                        action="submit", answers=[{"selected": [0], "note": QUESTION_NOTE}])
                else:
                    history.report["approval_response"] = await call("approve", approval_id=request["id"],
                        allow=not CASE.endswith("approval-denied"), remember=False)
        response = await task
        history.report["apply_response"] = response
        if not response["ok"]:
            history.report["refused_at"] = "apply"
            return
        receipt = response["result"]
        if receipt["status"] in {"accepted", "refused"}:
            settled_response = await call(SETTLED, check=False, **common,
                action_id=receipt["action_id"], operation_id=response["id"])
            history.report["settled_response"] = settled_response
            if settled_response["ok"]:
                fresh = (await call(history.SNAPSHOT, **history.common(app)))["result"]
                history.report["fresh"] = fresh
                assert fresh == settled_response["result"]["history"]
                history.report["page"] = (await call(history.PAGE, **history.common(app),
                    snapshot=fresh["snapshot"], anchor="tail"))["result"]
        if CASE == "duplicate-apply":
            before = stable(app)
            history.report["duplicate_response"] = await call(APPLY, check=False, **payload)
            assert stable(app) == before

    with ExitStack() as stack:
        for owner, name, callback in ((app, "exit", exit_app), (app.store, "release", release),
            (LiteTUI, "run_worker", launched), (rpc, "_dispatch", dispatch), (rpc, "rpc_emit", emit),
            (ask_user_question, "run", question), (app.store, "write_record", faulty_write),
            (app, "_ensure_chat_ready", ready), (app, "_clear_screen", clear_screen), (ttyguard, "run", command)):
            stack.enter_context(patch.object(owner, name, callback))
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
            (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(120), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                if CASE.startswith("owner-verified") and "--convo" in sys.argv:
                    from litetui import store_identity
                    history.report.update(archive_before=owned_transcript(app).read_bytes().hex(),
                        artifact=str(app._owner_memory_dir / "NOW.md"),
                        owner={"id": app._agent_session.authority.agent_id, "name": app._agent_session.authority.name,
                               "store": store_identity.capture(app), "session": app.convo_id, "privileged": bool(app._owner_seat)})
                else:
                    seed(app)
                if CASE.startswith("claude-segment"):
                    await seed_claude(app)
                seeded = True
                if CASE in {"readonly", "late-readonly"}:
                    readonly = owned_transcript(app)
                    if CASE == "readonly":
                        readonly.chmod(stat.S_IREAD)
                elif CASE == "pending-image":
                    app.pending_image = "NEVER CONSUME PENDING IMAGE"
                elif CASE == "unsupported-config":
                    app.settings.compact_max_tool_iters = 0
                elif CASE == "native-owned":
                    app.backend.owns_native_turns = True
                elif CASE == "native-app-server":
                    app.backend.app_server = object()
                elif CASE == "owner-mode":
                    app._owner_memory_dir = app._agent_session.memory_root
                history.report["baseline_live"] = copy.deepcopy(app.conversation)
                history.report["baseline"] = stable(app)
                await history.settle(app)
                if DRIVER == "rpc":
                    reader_start()
                    await asyncio.wait_for(exited.wait(), 100)
                else:
                    hello = (await call("gui.hello", protocol_version=1))["result"]
                    assert {SNAPSHOT, VALIDATE, APPLY, SETTLED} <= set(hello["operations"])
                    await self_driver()
                    if CASE == "native-owned":
                        del app.backend.owns_native_turns
                    elif CASE == "native-app-server":
                        del app.backend.app_server
                    elif CASE == "owner-mode":
                        app._owner_memory_dir = None
                    if not exited.is_set():
                        await shutdown()
                collect()
                history.report["wire_events"] = copy.deepcopy(history.boundary.records)
        finally:
            if readonly is not None:
                assert readonly.is_relative_to(history.root.resolve()) and readonly.name == "convo.jsonl"
                readonly.chmod(stat.S_IREAD | stat.S_IWRITE)
            if app.client is not None:
                await app.client.close()
            history.report["compact_workers"] = [{"name": w.name, "group": w.group, "finished": w.is_finished} for w in workers]
            history.report["streams"] = [{"kind": s.kind, "closed": s.closed} for s in streams]
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
                assert not reader.is_alive()


if CASE.startswith("codex-native"):
    history.case = CASE
    os.environ["CODEX_HOME"] = str(history.root / "isolated-codex")
    native_request = history.codex_request

    async def codex_request(server, method, params):
        if method in {"account/read", "model/list"}:
            return await native_request(server, method, params)
        assert history.explicit and params["threadId"] == "history-native-thread"
        history.report.setdefault("compaction_rpc", []).append({"method": method, "params": dict(params)})
        turn = "native-compact-turn"
        item = {"type": "contextCompaction", "id": "native-compact-item"}
        if method == "thread/compact/start":
            for event, fields in (("turn/started", {"turn": {"id": turn}}),
                                  ("item/started", {"turnId": turn, "item": item}),
                                  ("item/completed", {"turnId": turn, "item": item}),
                                  ("turn/completed", {"turn": {"id": turn, "status": "completed"}})):
                server.events.put_nowait({"method": event, "params": {"threadId": params["threadId"], **fields}})
            return {}
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "historyMode": "paginated"}}
        if method == "thread/turns/list":
            assert params["itemsView"] == "full" and params["sortDirection"] == "desc" and params["limit"] == 1
            items = [] if CASE == "codex-native-uncertain" else [item]
            return {"data": [{"id": turn, "itemsView": "full", "status": "completed", "items": items}]}
        raise AssertionError(method)

    history.codex_request = codex_request

history.completions = completions
history.serve = serve
history.prepare_hooks = lambda: None

if __name__ == "__main__":
    from unittest.mock import patch

    from litetui import plugins

    original_register = plugins.register_plugins

    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        return original_register(app, registry, order=("litetui.plugins.core_tools",
            "litetui.plugins.convo", "litetui.plugins.self_compact", "litetui.plugins.ask_user_question"), disabled=disabled)

    with patch.object(plugins, "register_plugins", controlled_register):
        if CASE.startswith("owner-verified"):
            spec = importlib.util.spec_from_file_location("_private_owner", Path(__file__).with_name("owner_handoff_fixture.py"))
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            with helper.configure(history):
                history.main()
        elif CASE.startswith("claude-segment"):
            import claude_agent_sdk

            from litetui import claude_compaction_evidence
            from litetui.claude_backend import ClaudeBackend
            async def metadata(backend):
                backend.models = {"claude-fixture": {"value": "claude-fixture"}}
                return "ok"
            verify_final = claude_compaction_evidence.Binding.verify_final

            def counted(self, app, receipt):
                result = verify_final(self, app, receipt)
                history.report["claude_verifications"] = history.report.get("claude_verifications", 0) + 1
                return result

            # The wake opens a NEW session through the real backend: its SDK client is the controlled one.
            with (patch.object(ClaudeBackend, "ensure_running", metadata),
                  patch.object(claude_agent_sdk, "ClaudeSDKClient", claude_client()),
                  patch.object(claude_compaction_evidence.Binding, "verify_final", counted)):
                history.main()
        else:
            history.main()
