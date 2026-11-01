"""Actual owned edit/retry RPC with controlled external SDK and OS boundaries."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import os
import socket
import stat
import threading
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch
from uuid import uuid4

spec = importlib.util.spec_from_file_location("_rewrite_history_support", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
case = os.environ.get("RUST_REWRITE_CASE", "edit")
driver = os.environ.get("RUST_REWRITE_DRIVER", "self")
SNAPSHOT = "gui.conversations.native_rewrite_snapshot"
VALIDATE = "gui.conversations.native_rewrite_validate"
APPLY = "gui.conversations.native_rewrite_apply"
SETTLED = "gui.conversations.native_rewrite_settled"
REPLACEMENT = "Explicit replacement: preserve prefix, discard only the reviewed suffix 雪"
TARGET = "Original selected user prompt"
TARGET_INDEX = 5
QUESTION = "question-" in case
QUESTION_COMPACTION = QUESTION and case.endswith("compaction")
AFTERCARE = {"auto-compact", "self-compact", "quit-compaction", "eof-compaction", "stop-compaction",
             "question-compaction", "quit-question-compaction", "eof-question-compaction", "stop-question-compaction"}
QUESTION_ARGS = {"questions": [{"label": "Rewrite choice", "question": "Which continuation is authorized?",
    "options": [{"title": "Keep reviewed context", "description": "Use the exact reviewed prefix"},
                {"title": "Discard task", "description": "Do not continue"}], "multiSelect": False}]}
QUESTION_NOTE = "Explicit controlled answer 雪"
QUESTION_RESULT = ("[ask_user_question] SUBMITTED — 1 of 1 answered\n\n"
    "  Rewrite choice — ANSWERED\n    [x] Keep reviewed context  (Use the exact reviewed prefix)\n"
    "    [ ] Discard task  (Do not continue)\n"
    "    note: " + QUESTION_NOTE + "\n\n"
    "These selections and notes are the user's answers — proceed on that basis.")
QUESTION_CALL: dict[str, Any] = {"id": "rewrite-question-call", "type": "function", "function": {
    "name": "ask_user_question", "arguments": json.dumps(QUESTION_ARGS)}}
question_threads: list[dict] = []
SUMMARY = "Controlled compacted continuity summary"
HANDOFF = "Continue the reviewed task from the retained tool result"
SELF_TEXT = "Controlled self-compaction handoff"
stream_kind = "turn"
readonly_target: Path | None = None
hook_processes: list[Any] = []
hook_documents: list[dict] = []
hook_entered = threading.Event()
stream_entered: asyncio.Event | None = None
stream_release: asyncio.Event | None = None



def _publish_stage(path, value):
    """Publish only complete JSON; existence is the cross-process ready signal."""
    pending = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        pending.write_text(json.dumps(value), encoding="utf-8")
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


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


def snapshot_records(app):
    records = []
    for line in owned_transcript(app).read_bytes().splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict) and row.get("type") == "snapshot":
            records.append(row)
    return records


def seed(app):
    global readonly_target
    from litetui import codex_steering, store_identity
    if case in AFTERCARE:
        assert "/compact" in app.plugins.commands
    app.conversation = [
        {"role": "system", "content": "REWRITE SYSTEM: preserve exact prefix context"},
        {"role": "user", "content": "PREFIX USER: keep this earlier request"},
        {"role": "assistant", "content": "PREFIX ASSISTANT: completed tool call",
         "tool_calls": [{"id": "rewrite-tool", "type": "function",
                         "function": {"name": "fixture_read", "arguments": "{}"}}]},
        {"role": "tool", "content": "PREFIX TOOL RESULT", "tool_call_id": "rewrite-tool"},
        {"role": "assistant", "content": "PREFIX FINAL ANSWER"},
        {"role": "user", "content": TARGET},
        {"role": "assistant", "content": "DISCARDED ASSISTANT SUFFIX",
         "reasoning_content": "DISCARDED REASONING"},
        {"role": "user", "content": "DISCARDED LATER USER"},
        {"role": "assistant", "content": "DISCARDED FINAL ANSWER"},
    ]
    if case == "orphan-tool":
        app.conversation[3]["tool_call_id"] = "unmatched-tool"
    elif case == "missing-tool-id":
        app.conversation[2]["tool_calls"][0].pop("id")
        app.conversation[3].pop("tool_call_id")
    elif case == "multimodal-target":
        app.conversation[TARGET_INDEX]["content"] = [{"type": "text", "text": TARGET},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,NEVER_LOAD"}}]
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
    assert not app.store.persist_error
    owned_transcript(app)
    if case == "held":
        entry = {"id": "rewrite-held", "threadId": "original-thread", "turnId": "original-turn",
                 "conversationId": app.convo_id, "state": "queued", "revision": 1,
                 "item": {"content": "HELD INPUT NEVER REPLAY", "source": "queued", "tool_profile": "strict"}}
        app._append({"role": "assistant", "content": "HELD DELIVERY LEDGER",
                     "provider_metadata": {"steering": [entry]}})
        codex_steering.restore_queue(app)
        assert app._pending_input and app._pending_input[0]["_restored_input_held"]
    elif case == "pending-image":
        app.pending_image = "NEVER_CONSUME_PENDING_IMAGE"
    elif case == "over-budget":
        app.ctx_max, app.ctx_loaded = 110, True
    elif case == "readonly":
        readonly_target = owned_transcript(app)
        readonly_target.chmod(stat.S_IREAD)
    history.report.update(original=copy.deepcopy(app.conversation),
                          prefix=copy.deepcopy(app.conversation[:TARGET_INDEX]),
                          before=stable(app), transcript_path=str(app.convo_path),
                          snapshots_before=snapshot_records(app))


class HookInput(io.StringIO):
    def close(self):
        if not self.closed:
            hook_documents.append(json.loads(self.getvalue()))
        super().close()


class HookProcess:
    def __init__(self, argv):
        assert argv == ["rewrite-fixture-hook", "check"]
        output = json.dumps({"decision": "deny", "reason": "controlled prompt denial"}) if case == "hooks-deny" else json.dumps({"decision": "allow"})
        self.stdin, self.stdout, self.stderr = HookInput(), io.StringIO(output), io.StringIO("")
        self.pid, self._litetui_job = 987654, None
        self.returncode = None if "admission" in case or case == "hooks-drift" else 0
        hook_entered.set()
        _publish_stage(history.root / "rewrite-stage.json", {"stage": "admission"})

    def poll(self):
        return self.returncode


def hook_spawn(argv, **_kwargs):
    process = HookProcess(argv)
    hook_processes.append(process)
    return process


def hook_kill(pid, process):
    assert process in hook_processes and pid == process.pid
    process.returncode = -15
    history.events.append({"effect": "rewrite-hook-kill"})
    return True


def prepare_hooks():
    if not (case.startswith("hooks-") or "admission" in case or case == "stale-hook-config"):
        return
    from litetui import tool_policy
    from litetui.lifecycle_hooks import Hook
    os.environ["LITETUI_HOOKS"] = "on"
    hook = Hook("rewrite-check", ("prompt_before",), "rewrite-fixture-hook", ("check",), mode="gate")
    (history.root / "hooks.json").write_text(json.dumps({"version": 1, "hooks": [hook.document()]}), encoding="utf-8")
    if case != "hooks-approval":
        path = history.root / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        workspace = Path.cwd().resolve()
        arguments = {"command": json.dumps([hook.executable, *hook.argv]), "cwd": str(workspace), "env": {}}
        settings["tool_always_allow"] = [tool_policy.rule_key(
            hook.approval_name(workspace), tool_policy.SHELL_POLICY.classify(arguments, workspace))]
        path.write_text(json.dumps(settings), encoding="utf-8")


original_completions = history.completions


async def completions(resource, **request):
    global stream_kind
    if not history.explicit:
        history.boundary.forbidden()
    app = history.boundary.app_instance
    from litetui.app import CARD_SUMMARY_PROMPT, COMPACT_PROMPT, WAKE_AFTER_COMPACT
    messages = request["messages"]
    kind = ("card-summary" if not request.get("stream") else "compaction"
            if isinstance(messages[-1].get("content"), str) and messages[-1]["content"].startswith(COMPACT_PROMPT)
            else "wake" if messages[-1].get("content") == WAKE_AFTER_COMPACT
            else "self-tool" if case == "self-compact" and not history.requests else "turn")
    if QUESTION and request.get("stream"):
        if messages[-1].get("role") == "tool" and messages[-1].get("tool_call_id") == QUESTION_CALL["id"]:
            kind = "compaction-followup" if QUESTION_COMPACTION else "question-followup"
        elif kind == "compaction":
            kind = "compaction-question"
        elif not QUESTION_COMPACTION and not history.requests:
            kind = "question-stream"
    assert resource._client is app.client and request["model"] == app._agent_session.authority.model
    assert "origin" not in request and "purpose" not in request
    history.report.setdefault("sdk_calls", []).append({"kind": kind, "stream": bool(request.get("stream")),
        "messages": len(messages), "context_sha256": history.sha(history.canonical(messages))})
    history.report.setdefault("all_sdk_requests", []).append({"kind": kind, "messages": copy.deepcopy(messages),
        "model": request["model"], "backend": app.backend.name, "session_id": app.convo_id,
        "agent_id": app._agent_session.authority.agent_id})
    assert not any("DISCARDED" in str(row) for row in messages)
    if kind == "card-summary":
        assert case == "self-compact", "Unexpected implicit nonstream SDK request"
        assert messages == [{"role": "user", "content": CARD_SUMMARY_PROMPT.replace("{answer}", SELF_TEXT)}]
        return NS(choices=[NS(message=NS(content="Controlled self-compaction card summary"))])
    stream_kind = kind
    if kind in {"question-followup", "compaction-followup"}:
        first = history.report["expected_compaction_request" if QUESTION_COMPACTION else "initial_prepared"]
        expected = [*copy.deepcopy(first), {"role": "assistant", "content": None, "tool_calls": [QUESTION_CALL]},
            {"role": "tool", "tool_call_id": QUESTION_CALL["id"], "name": "ask_user_question", "content": QUESTION_RESULT}]
        assert messages == expected, "The complete next request must contain only the exact question and explicit answer"
        history.report["expected_question_followup"] = expected
        if QUESTION_COMPACTION:
            stream = ControlledStream()
            history.boundary.streams.append(stream)
            return stream
        history.expected_saved_context = copy.deepcopy(app.conversation[:-1])
        return await original_completions(resource, **request)
    if kind in {"compaction", "compaction-question"}:
        assert case in AFTERCARE
        initial = history.report["initial_prepared"]
        expected_body = initial[1:] if case == "self-compact" else initial[1:-1]
        assert messages[1:-1] == expected_body, "Compaction must read the full ordered old head only"
        assert messages[0]["role"] == "system" and initial[0]["content"] in messages[0]["content"]
        extra = ("\n\nAgent's reason for compaction: Controlled reviewed self-compaction\n\n"
                 "Agent's continuity notes:\n" + HANDOFF) if case == "self-compact" else ""
        assert messages[-1] == {"role": "user", "content": COMPACT_PROMPT + extra}
        history.report["before_compaction"] = copy.deepcopy(app.conversation)
        history.report["expected_compaction_request"] = [copy.deepcopy(messages[0]), *copy.deepcopy(expected_body),
            {"role": "user", "content": COMPACT_PROMPT + extra}]
        stream = ControlledStream()
        history.boundary.streams.append(stream)
        return stream
    if kind == "wake":
        assert case in AFTERCARE and case not in {"quit-compaction", "eof-compaction", "stop-compaction"}
        summary = "[Summary of earlier conversation, which has been compacted away]\n\n" + SUMMARY
        if case == "self-compact":
            summary += "\n\n[Agent's pre-compaction handoff]\n" + HANDOFF
        expected_body = [{"role": "user", "content": summary},
            {"role": "assistant", "content": "Understood — I have that context."},
            *copy.deepcopy(history.report["before_compaction"][-2:]),
            {"role": "user", "content": WAKE_AFTER_COMPACT}]
        assert messages[1:] == expected_body, "Wake must read exact summary, verbatim tail and canonical wake user"
        assert messages[0]["role"] == "system" and history.report["prefix"][0]["content"] in messages[0]["content"]
        history.expected_saved_context = copy.deepcopy(app.conversation[:-1])
        history.report["expected_wake_request"] = [copy.deepcopy(messages[0]), *expected_body]
    else:
        assert messages[-1]["content"] == history.report["replacement"]
        history.report["initial_prepared"] = copy.deepcopy(history.prepared_context)
    return await original_completions(resource, **request)


history.prepare_hooks = prepare_hooks
history.completions = completions


async def until(predicate, timeout=15):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
    return predicate()


original_stream = history.boundary.Stream


class ControlledStream:
    def __init__(self):
        self.inner = original_stream()
        self.closed = False
        self.kind = stream_kind

    async def close(self):
        await self.inner.close()
        self.closed = self.inner.closed

    def __aiter__(self):
        async def chunks():
            held = "worker" in case or self.kind == "compaction" and case in {"quit-compaction", "eof-compaction", "stop-compaction"}
            if held:
                assert stream_entered is not None and stream_release is not None
                stream_entered.set()
                stage = "compaction" if self.kind == "compaction" else "worker"
                _publish_stage(history.root / "rewrite-stage.json", {"stage": stage})
                await stream_release.wait()
            if self.kind in {"question-stream", "compaction-question"}:
                call = NS(index=0, id=QUESTION_CALL["id"], type="function", function=NS(**QUESTION_CALL["function"]))
                yield NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=[call]), finish_reason=None)], usage=None)
                yield NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None), finish_reason="tool_calls")],
                         usage=NS(prompt_tokens=11, completion_tokens=5, total_tokens=16))
            elif self.kind in {"compaction", "compaction-followup"}:
                yield NS(choices=[NS(delta=NS(content=SUMMARY, reasoning_content=None, tool_calls=None), finish_reason=None)], usage=None)
                yield NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None), finish_reason="stop")],
                         usage=NS(prompt_tokens=30, completion_tokens=5, total_tokens=35))
            elif self.kind == "self-tool":
                call = NS(index=0, id="rewrite-self-call", type="function", function=NS(name="self_compact",
                    arguments=json.dumps({"reason": "Controlled reviewed self-compaction", "handoff": HANDOFF})))
                yield NS(choices=[NS(delta=NS(content=SELF_TEXT, reasoning_content=None, tool_calls=[call]), finish_reason=None)], usage=None)
                yield NS(choices=[NS(delta=NS(content=None, reasoning_content=None, tool_calls=None), finish_reason="tool_calls")],
                         usage=NS(prompt_tokens=11, completion_tokens=5, total_tokens=16))
            else:
                async for row in self.inner.__aiter__():
                    if self.kind == "turn" and case in AFTERCARE and row.usage is not None:
                        row.usage = NS(prompt_tokens=900000, completion_tokens=5, total_tokens=900005)
                    yield row
            if self.kind == "compaction-question":
                # The actual OpenAI AsyncStream closes its response on complete
                # exhaustion; the next compact tool round replaces this handle.
                # This is after normal exhaustion only, never cancellation.
                await self.close()
        return chunks()


async def serve(app):
    from litetui import ask_user_question, conversation_rewrite, rpc, ttyguard
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    global stream_entered, stream_release
    history.boundary.ready = asyncio.Event()
    stream_entered, stream_release = asyncio.Event(), asyncio.Event()
    exited = asyncio.Event()
    workers: list[Any] = []
    reader = None
    original_exit, original_release = app.exit, app.store.release
    original_dispatch, original_worker, original_emit = rpc._dispatch, LiteTUI.run_worker, rpc.rpc_emit
    serial = 0
    seeded = False
    original_question = ask_user_question.run

    def question(args, owner):
        assert owner is app and args == QUESTION_ARGS
        assert threading.current_thread() is not threading.main_thread()
        origin = conversation_rewrite.event_origin(app)
        assert origin is not None and origin.execution_id is not None
        assert not conversation_rewrite._authorized(app), "Event attribution must never grant execution admission"
        try:
            conversation_rewrite.require_open(app)
        except ValueError:
            pass
        else:
            raise AssertionError("Question thread acquired rewrite execution rights")
        row = {"thread_id": threading.get_ident(), "origin": asdict(origin), "retired": False}
        question_threads.append(row)
        try:
            row["result"] = original_question(args, owner)
            return row["result"]
        finally:
            row["retired"] = True

    def question_state():
        attempt = getattr(app, "_conversation_rewrite_attempt", None)
        history.report["question_threads"] = copy.deepcopy(question_threads)
        history.report["question_pending"] = list(getattr(app, "_rpc_pending_asks", {}))
        history.report["question_futures"] = [{"done": call.future.done(), "cancelled": call.future.cancelled(),
            "producer_done": call.producer.done(), "callback_retired": call.retired.is_set(),
            "lifetime_cancelled": call.cancelled.is_set(), "origin": asdict(call.origin)}
            for call in (getattr(attempt, "tool_calls", None) or ())]

    def exit_app(*args, **kwargs):
        if seeded:
            question_state()
            history.report["after"] = stable(app)
            history.report["durable"] = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1]
            history.report["live"] = copy.deepcopy(app.conversation)
            history.report["persist_error"] = app.store.persist_error
            history.report["snapshots_after"] = snapshot_records(app)
        exited.set()
        return original_exit(*args, **kwargs)

    def release():
        if seeded:
            if QUESTION:
                question_state()
                assert question_threads and all(row["retired"] for row in question_threads)
                assert not history.report["question_pending"]
                assert all(row["done"] and row["producer_done"] and row["callback_retired"]
                           for row in history.report["question_futures"])
            active = [worker.name for worker in workers if worker.group == "chat" and not worker.is_finished]
            history.events.append({"effect": "rewrite-lease-release", "active_chat": active,
                                   "active_hooks": sum(process.poll() is None for process in hook_processes)})
            assert not active and not any(process.poll() is None for process in hook_processes)
        return original_release()

    def launched(self, *args, **kwargs):
        worker = original_worker(self, *args, **kwargs)
        workers.append(worker)
        return worker

    def dispatch(self, command):
        history.commands.append(copy.deepcopy(command))
        if command["type"] == APPLY:
            history.explicit = True
            history.expected_saved_context = copy.deepcopy(history.report["prefix"])
            history.report["replacement"] = command["text"]
            if driver == "rpc":
                history.report["before_apply"] = stable(app)
                history.report["baseline_live"] = copy.deepcopy(app.conversation)
        return original_dispatch(self, command)

    def emit(row):
        release_compaction = False
        if driver == "rpc" and row.get("type") == "response":
            command = next((item for item in history.commands if item.get("id") == row.get("id")), None)
            wire = len(json.dumps(row).encode("utf-8"))
            assert wire <= history.MAX_WIRE
            history.observations.append({"type": command["type"] if command else None, "wire_bytes": wire})
            history.report.setdefault("responses", []).append(copy.deepcopy(row))
            release_compaction = (case == "stop-compaction" and command is not None
                and command["type"] == "abort" and row.get("ok") is True
                and row.get("result", {}).get("stopped") is True)
            if command is not None and row.get("ok"):
                field = {SNAPSHOT: "snapshot", VALIDATE: "review", APPLY: "apply", SETTLED: "settled"}.get(command["type"])
                if field:
                    history.report[field] = copy.deepcopy(row["result"])
                if command["type"] == VALIDATE:
                    history.report["candidate"] = [*copy.deepcopy(history.report["prefix"]),
                        {"role": "user", "content": row["result"]["text"]}]
        result = original_emit(row)
        if release_compaction:
            # A soft Stop is observed at the next provider chunk in both drivers.
            assert stream_release is not None
            stream_release.set()
            history.report["compaction_abort_released"] = True
        if QUESTION and row.get("type") == "gui.event" and row["event"]["type"] == "user_input_requested":
            history.report["question_request"] = copy.deepcopy(row)
            stage = "question-compaction" if QUESTION_COMPACTION else "question-stream"
            _publish_stage(history.root / "rewrite-stage.json", {"stage": stage,
                "ask_id": row["event"]["id"], "execution_id": row["execution_id"]})
        return result

    async def call(operation, *, check=True, **fields):
        nonlocal serial
        serial += 1
        command = {"type": operation, "id": f"rewrite-{serial}", **fields}
        rpc._dispatch(app, command)
        response = await until(lambda: next((row for row in history.boundary.records
            if row.get("type") == "response" and row.get("id") == command["id"]), None), 30)
        history.report.setdefault("responses", []).append(copy.deepcopy(response))
        wire = len(json.dumps(response).encode("utf-8"))
        assert wire <= history.MAX_WIRE
        history.observations.append({"type": operation, "wire_bytes": wire})
        if check:
            assert response["ok"], response
        return response

    def reader_start():
        nonlocal reader
        assert reader is None
        reader = threading.Thread(target=rpc._reader_loop, args=(app,), name="rewrite-real-rpc-stdin", daemon=True)
        reader.start()

    async def finish_attempt(response, common):
        receipt = response.get("result", {})
        history.report["apply_response"] = response
        history.report["apply"] = receipt
        if receipt:
            assert receipt["operation_id"] == response["id"]
        if receipt.get("turn") == "accepted":
            await until(lambda: any(row.get("type") == "turn_end" for row in history.boundary.records), 20)
        if receipt.get("turn") in {"accepted", "refused"}:
            settled = await call(SETTLED, **common, action_id=receipt["action_id"], operation_id=response["id"])
            history.report["settled"] = settled["result"]
            fresh = (await call(history.SNAPSHOT, **history.common(app)))["result"]
            assert fresh == settled["result"]["history"]
            history.report["fresh"] = fresh
            history.report["tail"] = (await call(history.PAGE, **history.common(app),
                snapshot=fresh["snapshot"], anchor="tail"))["result"]

    async def self_driver():
        nonlocal serial
        common = {"version": 1, "session_id": app.convo_id,
                  "agent_id": app._agent_session.authority.agent_id, "backend": app.backend.name, "model": app.model_id}
        descriptor = (await call(history.SNAPSHOT, **history.common(app)))["result"]
        selection = {"history_snapshot": descriptor["snapshot"], "context_sha256": descriptor["context_sha256"],
                     "message_index": TARGET_INDEX,
                     "message_sha256": history.sha(history.canonical(app.conversation[TARGET_INDEX]))}
        if case == "non-user":
            selection.update(message_index=4, message_sha256=history.sha(history.canonical(app.conversation[4])))
        elif case == "stale-message":
            selection["message_sha256"] = "0" * 64
        elif case == "foreign-session":
            common["session_id"] = "42345678-1234-4123-8123-123456789abc"
        chosen = {**common, **selection}
        response = await call(SNAPSHOT, check=False, **chosen)
        history.report["snapshot_response"] = response
        if not response["ok"]:
            history.report["refused_at"] = "snapshot"
            return
        snapshot = response["result"]
        history.report["snapshot"] = snapshot
        if not snapshot["actions"]["edit"]["eligible"]:
            history.report["refused_at"] = "eligibility"
            assert snapshot["actions"]["edit"]["reason"]
            return
        action = "retry" if case == "retry" else "edit"
        text = (TARGET if action == "retry" else "  雪🛰️e\u0301\r\nExact trailing spaces  " if case == "unicode"
                else "x" * 65536 if case == "text-boundary" else "x" * 65537 if case == "text-over-limit"
                else "/new" if case == "slash" else "missing.png" if case == "image-path"
                else " \n\t" if case == "empty" else REPLACEMENT)
        validate = {**chosen, "snapshot": snapshot["snapshot"], "action": action}
        if action == "edit":
            validate["text"] = text
        response = await call(VALIDATE, check=False, **validate)
        history.report["validate_response"] = response
        if not response["ok"]:
            history.report["refused_at"] = "validate"
            return
        review = response["result"]
        history.report["review"] = review
        assert review["text"] == text
        expected = [*history.report["prefix"], {"role": "user", "content": text}]
        assert review["prepared_context_sha256"] == history.sha(history.canonical(expected))
        history.report["candidate"] = expected
        payload = {key: value for key, value in review.items() if key not in {"kind", "effects"}}
        if case == "stale-history":
            app._append({"role": "assistant", "content": "External canonical history change"})
        elif case == "stale-settings":
            app.settings.max_tokens_chat += 1
        elif case == "stale-hook-config":
            (history.root / "hooks.json").write_text('{"version":1,"hooks":[]}', encoding="utf-8")
        elif case == "wrong-echo":
            payload["text_sha256"] = "0" * 64
        elif case == "bool-version":
            payload["version"] = True
        elif case == "float-index":
            payload["message_index"] = float(TARGET_INDEX)
        elif case == "superseded-review":
            newer = (await call(VALIDATE, **validate))["result"]
            assert newer["review"] != review["review"] and newer["action_id"] != review["action_id"]
        history.report["before_apply"] = stable(app)
        history.report["baseline_live"] = copy.deepcopy(app.conversation)
        if QUESTION:
            if case.startswith("eof-"):
                reader_start()
            task = asyncio.create_task(call(APPLY, check=False, **payload))
            requested = await until(lambda: next((row for row in history.boundary.records
                if row.get("type") == "gui.event" and row["event"]["type"] == "user_input_requested"), None), 25)
            history.report["question_request"] = copy.deepcopy(requested)
            if case.startswith("stop-"):
                stopped = await call("abort")
                assert stopped["result"]["stopped"]
                assert stopped["result"]["cancelled_asks"] == [requested["event"]["id"]]
                history.report["question_stop"] = stopped
                await finish_attempt(await task, common)
            elif not case.startswith(("quit-", "eof-")):
                answer = await call("answer", ask_id=requested["event"]["id"], action="submit",
                                    answers=[{"selected": [0], "note": QUESTION_NOTE}])
                history.report["question_answer"] = answer
                await finish_attempt(await task, common)
            else:
                if case.startswith("quit-"):
                    rpc._dispatch(app, {"type": "shutdown", "id": "rewrite-shutdown"})
                await asyncio.wait_for(exited.wait(), 20)
                response = await asyncio.wait_for(asyncio.shield(task), 5)
                history.report["apply_response"], history.report["apply"] = response, response.get("result", {})
                shutdown_task = app._conversation_rewrite_shutdown_task
                assert shutdown_task.done() and not shutdown_task.cancelled()
                history.report["shutdown"] = shutdown_task.result()
            question_state()
            return
        if "admission" in case or "worker" in case or case == "hooks-drift" or case in {"quit-compaction", "eof-compaction", "stop-compaction"}:
            if case.startswith("eof-"):
                reader_start()
            task = asyncio.create_task(call(APPLY, check=False, **payload))
            if "worker" in case or case.endswith("compaction"):
                await asyncio.wait_for(stream_entered.wait(), 25)
            else:
                assert await asyncio.to_thread(hook_entered.wait, 15)
            if case == "hooks-drift":
                app.settings.max_tokens_chat += 1
                history.report["before_apply"]["settings"] = asdict(app.settings)
                for process in hook_processes:
                    process.returncode = 0
                response = await task
                await finish_attempt(response, common)
            else:
                if case == "stop-compaction":
                    response = await task
                    stopped = await call("abort")
                    assert stopped["result"]["stopped"]
                    stream_release.set()  # Canonical soft Stop is observed at the next provider chunk.
                    await finish_attempt(response, common)
                    return
                if case == "cancel-admission":
                    app._conversation_rewrite_attempt.owner.cancel()
                    response = await task
                    await finish_attempt(response, common)
                    return
                if case.startswith("quit-"):
                    rpc._dispatch(app, {"type": "shutdown", "id": "rewrite-shutdown"})
                await asyncio.wait_for(exited.wait(), 20)
                response = await asyncio.wait_for(asyncio.shield(task), 5)
                history.report["apply_response"] = response
                history.report["apply"] = response.get("result", {})
                shutdown_task = app._conversation_rewrite_shutdown_task
                assert shutdown_task.done() and not shutdown_task.cancelled()
                history.report["shutdown"] = shutdown_task.result()
                if case.startswith("quit-"):
                    acknowledgement = next(row for row in history.boundary.records
                        if row.get("type") == "response" and row.get("id") == "rewrite-shutdown")
                    assert acknowledgement["ok"]
                    assert acknowledgement["result"]["conversation_rewrite_shutdown"] == shutdown_task.result()
                    history.report["shutdown_response"] = acknowledgement
            return
        with ExitStack() as faults:
            if case == "schedule-fail":
                def no_worker(**_kwargs):
                    raise RuntimeError("Controlled failure scheduling the canonical worker")
                faults.enter_context(patch.object(app, "_stream", no_worker))
            if case in {"persist-silent", "persist-torn", "persist-wrong", "persist-receipts"}:
                original_write = app.store.write_record
                def snapshot_write(record):
                    assert record["type"] == "snapshot"
                    history.report["snapshot_attempts"] = history.report.get("snapshot_attempts", 0) + 1
                    if case == "persist-silent":
                        return
                    if case == "persist-torn":
                        with owned_transcript(app).open("ab") as handle:
                            handle.write(b'\n{"type":"snapshot","messages":[')
                        app.store._raise_to_app(OSError("Controlled torn snapshot append"))
                        return
                    wrong = copy.deepcopy(record)
                    if case == "persist-receipts":
                        wrong["store_injected"] = not wrong["store_injected"]
                    else:
                        wrong["messages"][-1]["content"] = "CORRUPTED SNAPSHOT USER"
                    original_write(wrong)
                faults.enter_context(patch.object(app.store, "write_record", snapshot_write))
            response = await call(APPLY, check=False, **payload)
            await finish_attempt(response, common)
        if history.report.get("apply", {}).get("close_required"):
            blocked = await call(history.SNAPSHOT, check=False, **history.common(app))
            history.report["blocked_after_failure"] = blocked
            assert not blocked["ok"]
        if case == "replayed-apply":
            before = stable(app)
            replay = await call(APPLY, check=False, **payload)
            history.report["replay"] = replay
            assert not replay["ok"] or replay["result"]["turn"] != "accepted"
            assert stable(app) == before

    with ExitStack() as stack:
        stack.enter_context(patch.object(app, "exit", exit_app))
        stack.enter_context(patch.object(app.store, "release", release))
        stack.enter_context(patch.object(LiteTUI, "run_worker", launched))
        stack.enter_context(patch.object(rpc, "_dispatch", dispatch))
        stack.enter_context(patch.object(rpc, "rpc_emit", emit))
        stack.enter_context(patch.object(ttyguard, "popen", hook_spawn))
        if QUESTION:
            stack.enter_context(patch.object(ask_user_question, "run", question))
        stack.enter_context(patch.object(ttyguard, "kill_tree", hook_kill))
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(120), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                seed(app)
                history.report["sdk_calls"] = []
                history.report["all_sdk_requests"] = []
                seeded = True
                await history.settle(app)
                history.report["ready"] = next(row for row in history.boundary.records if row.get("type") == "ready")
                if driver == "rpc":
                    reader_start()
                    await asyncio.wait_for(exited.wait(), 100)
                    task = getattr(app, "_conversation_rewrite_shutdown_task", None)
                    if task is not None:
                        assert task.done() and not task.cancelled()
                        history.report["shutdown"] = task.result()
                else:
                    hello = (await call("gui.hello", protocol_version=1))["result"]
                    assert {SNAPSHOT, VALIDATE, APPLY, SETTLED} <= set(hello["operations"]), "Source omitted reviewed rewrite protocol"
                    await self_driver()
                if not exited.is_set():
                    history.report["after"] = stable(app)
                    history.report["persist_error"] = app.store.persist_error
                    history.report["durable"] = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1]
                    history.report["live"] = copy.deepcopy(app.conversation)
                    history.report["snapshots_after"] = snapshot_records(app)
                history.report["hook_documents"] = hook_documents
                history.report["wire_events"] = copy.deepcopy(history.boundary.records)
        finally:
            if readonly_target is not None:
                assert readonly_target.is_relative_to(history.root.resolve()) and readonly_target.name == "convo.jsonl"
                readonly_target.chmod(stat.S_IREAD | stat.S_IWRITE)
            await app.client.close()
            history.report["rewrite_workers"] = [{"name": worker.name, "group": worker.group,
                "finished": worker.is_finished} for worker in workers]
            history.report["hook_processes"] = [{"returncode": process.poll()} for process in hook_processes]
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
                assert not reader.is_alive()


history.boundary.Stream = ControlledStream
history.serve = serve
if case in AFTERCARE or QUESTION:
    from litetui import plugins
    original_register = plugins.register_plugins
    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        return original_register(app, registry, order=("litetui.plugins.core_tools",
            "litetui.plugins.self_compact", "litetui.plugins.convo")
            + (("litetui.plugins.ask_user_question",) if QUESTION else ()), disabled=disabled)
    with patch.object(plugins, "register_plugins", controlled_register):
        history.main()
else:
    history.main()
