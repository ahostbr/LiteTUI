"""Actual owned history projection and paging with external boundaries controlled.

The real CLI, replay store, authority checks, GUI dispatcher, transport adapter and
Textual application run. Only HTTP/OS/provider boundaries are synthetic. Saved
history is read-only; the fixture makes a separate explicit ordinary prompt.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import os
import platform
import socket
import stat
import subprocess
import sys
import threading
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_HISTORY_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ["RUST_BACKEND_SWITCH_SOURCE"] = str(source)
os.environ["RUST_BACKEND_SWITCH_CASE"] = "success"
spec = importlib.util.spec_from_file_location(
    "_history_external_boundaries", Path(__file__).with_name("backend_switch_runtime.py"))
assert spec and spec.loader
boundary: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
case = os.environ.get("RUST_HISTORY_CASE", "many")
driver = os.environ.get("RUST_HISTORY_DRIVER", "self")
phase = os.environ.get("RUST_HISTORY_PHASE", "fresh")
SNAPSHOT = "gui.history.native_snapshot"
PAGE = "gui.history.native_page"
OPEN = "gui.conversations.native_open"
EXPLICIT = "Explicit prompt after saved history inspection"
MAX_WIRE = 1024 * 1024
requests: list[dict] = []
commands: list[dict] = []
observations: list[dict] = []
reads: list[dict] = []
bubbles: list[dict] = []
report: dict[str, Any] = {}
explicit = False
expected_saved_context: list[dict] = []
prepared_context: list[dict] = []
target: str | None = None
target_path: Path | None = None
events: list[dict] = []
hook_processes: list[Any] = []
hook_entered = threading.Event()
provider_calls: list[dict] = []
provider_requests: list[dict] = []
provider_hydration_entered = asyncio.Event()
provider_hydration_release = asyncio.Event()
provider_hydration_active = False
provider_history_closing_cases = {"codex-quit-during-hydration", "codex-eof-during-hydration"}
provider_connect_closing_cases = {"codex-quit-during-connect", "codex-eof-during-connect"}
provider_closing_cases = provider_history_closing_cases | provider_connect_closing_cases
worker_handles: list[Any] = []
closing_cases = {"quit-during-open", "eof-during-open"}
shutdown_cases = closing_cases | provider_closing_cases
hook_cases = closing_cases | {"hooks-success", "hooks-failed-leave", "hooks-failed-resume", "hooks-approval", "hooks-drift"}
open_cases = {"preview-open", "lmstudio-open", "preview-readonly", "open-stale", "open-readback-drift", "codex-open", "codex-open-history-error"} | hook_cases | provider_closing_cases


def sha(raw: bytes | bytearray) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def pending_input_identity(app):
    from litetui.codex_steering import SteeringLedger

    items = []
    for item in app._pending_input:
        row = copy.deepcopy({key: value for key, value in item.items() if key != "_codex_ledger"})
        row["fixture_item_id"] = id(item)
        if "_codex_ledger" in item:
            ledger = item["_codex_ledger"]
            assert isinstance(ledger, SteeringLedger)
            row["_codex_ledger"] = {"object_id": id(ledger), "entries_id": id(ledger.entries),
                                    "save_id": id(ledger.save), "entries": copy.deepcopy(ledger.entries)}
            row["fixture_entry_id"] = id(item["_codex_entry"])
        items.append(row)
    return items


def identity(app):
    return {**boundary.identity(app), "tools_enabled": app.tools_enabled,
            "profile": app._active_tool_profile, "context_sha256": sha(canonical(app.conversation)),
            "messages": len(app.conversation), "pending": app.store.pending,
            "queue": pending_input_identity(app)}


def disk(app):
    directory = app._agent_session.memory_root
    paths = [root / "settings.json", *directory.rglob("*")]
    return {str(path.relative_to(root)): sha(path.read_bytes())
            for path in paths if path.is_file() and not path.name.endswith((".lock", ".lease"))}


def saved_messages(shape: str):
    head = [{"role": "system", "content": "History fixture system"}]
    if shape == "codex":
        return head + [{"role": "user", "content": "Saved native user"},
                       {"role": "assistant", "content": "Saved native fallback",
                        "provider_metadata": {"provider": "codex", "app_server_thread_id": "history-native-thread",
                                              "app_server_turn_id": "history-native-turn",
                                              "display_trace": {"version": 1, "items": [
                                                  {"id": "native-tool", "turnId": "history-native-turn",
                                                   "kind": "commandExecution", "name": "shell",
                                                   "args": "echo old", "state": "running", "result": "old partial"}]}}}]
    if shape == "many":
        return head + [row for index in range(601) for row in (
            {"role": "user", "content": f"Saved user {index}: repeated prompt"},
            {"role": "assistant", "content": f"Saved answer {index}"})]
    if shape in {"giant", "wire-heavy", "astral"}:
        unit = "giant text 雪\r\n" if shape == "giant" else "\x00\x1b\u202e\r\n" if shape == "wire-heavy" else "🛰️雪e\u0301\n"
        return head + [{"role": "user", "content": "Large saved block"},
                       {"role": "assistant", "content": unit * 5000}]
    if shape == "repeated":
        return head + [{"role": "user", "content": "identical prompt"},
                       {"role": "assistant", "content": "first saved answer"},
                       {"role": "user", "content": "identical prompt"},
                       {"role": "assistant", "content": "second saved answer"}]
    if shape == "rich":
        trace: dict[str, Any] = {"app_server_thread_id": "saved-codex-thread",
                 "display_trace": {"version": 1, "items": [
                     {"id": "agent-1", "turnId": "saved-turn", "kind": "agentMessage",
                      "result": "Codex shown answer", "state": "complete"},
                     {"id": "plan-1", "turnId": "saved-turn", "kind": "plan",
                      "name": "Saved plan", "result": "1. Inspect\n2. Explain", "state": "complete"},
                     {"id": "tool-1", "turnId": "saved-turn", "kind": "commandExecution",
                      "name": "shell", "args": "echo saved", "result": "old partial",
                      "state": "running"},
                     {"id": "tool-2", "turnId": "saved-turn", "kind": "commandExecution",
                      "name": "shell", "args": "false", "result": "saved failure",
                      "state": "failed", "ok": False, "durationMs": 12}]}}
        trace["async_questions"] = [
            {"version": 1, "id": "saved-question", "threadId": "saved-codex-thread",
             "turnId": "saved-turn", "itemId": "question-1", "revision": 1, "state": "pending",
             "questions": [{"title": "Stale saved question", "options": ["Old option"]}]},
            {"version": 1, "id": "terminal-question", "threadId": "saved-codex-thread",
             "turnId": "saved-turn", "itemId": "question-2", "revision": 1, "state": "pending",
             "questions": [{"title": "Terminal saved question", "options": ["Never request this"]}]},
        ]
        latest_trace = copy.deepcopy(trace)
        latest_trace["async_questions"][0].update(revision=2, questions=[{"title": "Latest saved question", "options": ["Saved option"]}])
        latest_trace["async_questions"][1].update(revision=2, state="answered")
        return head + [
            {"role": "user", "content": [{"type": "text", "text": "Saved multimodal text"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,NOT_REAL_IMAGE"}},
                {"type": "image_url", "image_url": {"url": "https://invalid.example/never-fetch.png"}}]},
            {"role": "assistant", "content": "Generic shown answer", "reasoning_content": "Saved reasoning only"},
            {"role": "assistant", "content": "Hidden duplicate Codex fallback", "provider_metadata": trace},
            {"role": "assistant", "content": "", "provider_metadata": latest_trace},
            {"role": "assistant", "content": "First card\n\nSecond card",
             "claude_native": {"card_text_lengths": [10, 11], "activities": [
                 {"kind": "tool_use", "id": "claude-tool", "name": "Read", "input": {"path": "saved.txt"}, "card": 0},
                 {"kind": "tool_result", "id": "claude-tool", "name": "Read", "result": "saved file", "card": 0}]}},
            {"role": "assistant", "content": "Recap answer\n<recap>Saved final recap</recap>"},
            {"role": "tool", "content": "Generic saved tool output", "tool_call_id": "generic-tool"},
            {"role": "assistant", "content": [{"type": "unrecognized_saved", "opaque": "kept inert"},
                {"type": "image", "source": {"type": "base64", "data": "NEVER_EMIT_UNKNOWN_BASE64"}}]},
        ]
    return head + [{"role": "user", "content": "Saved original prompt"},
                   {"role": "assistant", "content": "Saved original answer"}]


def seed(app):
    from litetui import store_identity
    from litetui.conversation import ConversationRepository

    global target, target_path
    if phase != "fresh" or case == "pending":
        return
    app.conversation = saved_messages("many" if case in {"one-shot-stale", "cancel-page", "span-drift", "order-drift", "cursor-loop", "restart-cursor"} else case)
    if case == "codex-inspect":
        app.conversation = saved_messages("codex")
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity,
                              store_injected=True)
    if case == "rich":
        app.store.record_card_summary(app._card_summary_key("Generic shown answer"), "Saved external summary")
    if case in {"mixed", "torn"}:
        app.store.record_edit(1, {"role": "user", "content": "Edited canonical prompt"})
        app.store.record_truncate(2, [{"role": "user", "content": "Truncated canonical prefix"}], True)
        app.store.record_msg({"role": "assistant", "content": "Final canonical answer"})
        app.store.write_record({"type": "rename", "name": "Canonical renamed history"})
        if case == "torn":
            with app.convo_path.open("ab") as handle:
                handle.write(b'\n{"type":"msg","message":{"role":"assistant","content":"torn \xf0\x9f')
        _, app.conversation = ConversationRepository.read(app.convo_path)
    if case == "source-budget":
        with app.convo_path.open("ab") as handle:
            handle.write(b'\n{"ignored":"' + b"x" * (8 * 1024 * 1024) + b'"}\n')
    if case in open_cases:
        repository = ConversationRepository(agent_session=app._agent_session)
        repository.stage(str(uuid.uuid4()))
        repository.acquire()
        repository.pending = False
        repository.record_meta(app.model_id, app._agent_session.authority.name, app._agent_session.authority.agent_id)
        repository.record_snapshot(saved_messages("codex" if case.startswith("codex-") else "many"), store_identity=app._prompt_store_identity,
                                   store_injected=True)
        repository.write_record({"type": "rename", "name": "Paged target 雪"})
        target = repository.convo_id
        target_path = repository.convo_path
        repository.release()
        assert target_path is not None
        assert target_path.resolve().is_relative_to(root.resolve())
        assert target_path.name == "convo.jsonl" and target_path.parent.name == target
        if case == "preview-readonly":
            target_path.chmod(stat.S_IREAD)
    assert not app.store.persist_error


def lmstudio_models(url, body=None, **_kwargs):
    if not case.startswith("lmstudio-") or body is not None or url != "http://127.0.0.1:45120/api/v0/models":
        return boundary.forbidden()
    provider_calls.append({"method": "models/read", "url": url, "explicit": explicit})
    return {"data": [{"id": "switch-old", "type": "llm", "loaded_context_length": 1000000}]}


async def codex_start(server):
    if server.process is None:
        server.process = NS(returncode=None)
        server.native_bridge = NS(policy=None)
    server.initialized = True


async def codex_close(server):
    if server.process is not None:
        server.process.returncode = 0
    server.initialized = False
    server.runtime_activity.disconnected()


async def hold_provider_read():
    global provider_hydration_active
    provider_hydration_active = True
    events.append({"effect": "provider-entered"})
    provider_hydration_entered.set()
    try:
        await provider_hydration_release.wait()
    finally:
        provider_hydration_active = False
        events.append({"effect": "provider-settled"})


async def codex_request(server, method, params):
    provider_calls.append({"method": method, "params": copy.deepcopy(params), "explicit": explicit})
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        if case in provider_connect_closing_cases and sum(row["method"] == "model/list" for row in provider_calls) > 1:
            await hold_provider_read()
        return {"data": [{"model": "gpt-6.1-sol", "displayName": "Controlled Codex",
                          "defaultReasoningEffort": "high",
                          "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
    if method == "thread/read":
        assert params["threadId"] == "history-native-thread"
        if case in provider_history_closing_cases:
            await hold_provider_read()
        if case == "codex-open-history-error":
            from litetui.model_transport import ProviderError
            raise ProviderError("Controlled native history read failure")
        return {"thread": {"id": "history-native-thread", "historyMode": "paginated"}}
    if method == "thread/turns/list":
        assert params["threadId"] == "history-native-thread"
        if params.get("cursor") is None:
            return {"data": [{"id": "history-native-turn", "status": "completed", "itemsView": "full",
                              "items": [{"id": "native-tool", "type": "commandExecution",
                                         "command": "echo old", "status": "completed", "exitCode": 0,
                                         "aggregatedOutput": "Hydrated saved tool result", "durationMs": 9},
                                        {"id": "native-answer", "type": "agentMessage",
                                         "text": "Hydrated saved answer"}]}], "nextCursor": "controlled-second-page"}
        assert params["cursor"] == "controlled-second-page"
        return {"data": [], "nextCursor": None}
    if method == "thread/resume":
        assert explicit and params["threadId"] == "history-native-thread"
        return {"thread": {"id": params["threadId"], "turns": []}}
    if method == "turn/start":
        assert explicit and params["threadId"] == "history-native-thread"
        app = boundary.app_instance
        provider_requests.append({"session_id": app.convo_id, "thread_id": params["threadId"],
                                  "input": copy.deepcopy(params["input"]), "model": params["model"]})
        turn = "history-explicit-turn"
        for event in [
            {"method": "item/agentMessage/delta", "params": {"threadId": params["threadId"], "turnId": turn,
             "itemId": "history-explicit-answer", "delta": "Controlled explicit native answer"}},
            {"method": "turn/completed", "params": {"threadId": params["threadId"],
             "turn": {"id": turn, "status": "completed"}}},
        ]:
            await server.events.put(event)
        return {"turn": {"id": turn}}
    raise AssertionError("Unexpected native provider boundary: " + method)


class HookInput(io.StringIO):
    def close(self):
        if not self.closed:
            events.append({"effect": "hook", "document": json.loads(self.getvalue())})
        super().close()


class HookProcess:
    def __init__(self, argv):
        assert argv == ["history-fixture-hook", "observe"]
        self.stdin, self.stdout, self.stderr = HookInput(), io.StringIO(""), io.StringIO("")
        self.pid, self._litetui_job = 987653, None
        self.returncode = None if case in closing_cases else 0
        if case in closing_cases:
            hook_entered.set()
            (root / "history-hook-entered").write_text("owned hook started", encoding="utf-8")

    def poll(self):
        if case in closing_cases:
            return self.returncode
        delivered = [row["document"] for row in events if row["effect"] == "hook"]
        failed = ("conversation_leave" if case == "hooks-failed-leave" else
                  "conversation_resume" if case == "hooks-failed-resume" else None)
        return 7 if delivered and delivered[-1]["event"] == failed else 0


def hook_spawn(argv, **_kwargs):
    process = HookProcess(argv)
    hook_processes.append(process)
    return process


def hook_kill(pid, process):
    assert process in hook_processes and pid == process.pid
    events.append({"effect": "hook-kill"})
    process.returncode = -15
    return True


def prepare_hooks():
    if case not in hook_cases:
        return
    from litetui.lifecycle_hooks import Hook, HookConfig
    hook = Hook("history-observer", ("conversation_leave", "conversation_resume"),
                "history-fixture-hook", ("observe",), mode="observe")
    (root / "hooks.json").write_text(json.dumps({"version": 1, "hooks": [hook.document()]}), encoding="utf-8")
    assert HookConfig(root / "hooks.json", Path.cwd() / ".litetui/hooks.json").read("global") == (hook,)
    if case != "hooks-approval":
        from litetui import tool_policy
        path = root / "settings.json"
        settings = json.loads(path.read_text(encoding="utf-8"))
        workspace = Path.cwd().resolve()
        arguments = {"command": json.dumps([hook.executable, *hook.argv]), "cwd": str(workspace), "env": {}}
        settings["tool_always_allow"] = [tool_policy.rule_key(
            hook.approval_name(workspace), tool_policy.SHELL_POLICY.classify(arguments, workspace))]
        path.write_text(json.dumps(settings), encoding="utf-8")


async def completions(resource, **request):
    """External SDK request seam; the actual transport still strips private metadata."""
    app = boundary.app_instance
    assert app is not None and explicit, "History reads must never call inference or summaries"
    assert resource._client is app.client
    assert request["model"] == app._agent_session.authority.model
    assert "origin" not in request and "purpose" not in request
    if not request.get("stream"):
        return NS(choices=[NS(message=NS(content="Controlled explicit-turn summary"))])
    messages = request["messages"]
    expected = [{key: value for key, value in row.items() if key not in {"provider_metadata", "codex_delivery"}}
                for row in prepared_context]
    assert messages == expected, "The actual adapter must send the complete canonical prepared request in exact order"
    assert len(prepared_context) == len(expected_saved_context) + 1
    for saved, prepared in zip(expected_saved_context, prepared_context, strict=False):
        if saved.get("role") == "system" and isinstance(saved.get("content"), str):
            # Canonical request preparation may append its once-only project/store block.
            assert prepared["role"] == "system" and saved["content"] in prepared["content"]
            assert {key: value for key, value in prepared.items() if key != "content"} == {
                key: value for key, value in saved.items() if key != "content"}
        else:
            assert saved == prepared, "Saved assistant/tool/user/provider rows changed before the explicit request"
    assert all("provider_metadata" not in row and "codex_delivery" not in row for row in messages)
    requests.append({"agent_id": app._agent_session.authority.agent_id, "session_id": app.convo_id,
                     "backend": app.backend.name, "model": request["model"],
                     "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled,
                     "messages": len(messages), "context_sha256": sha(canonical(messages)),
                     "prepared_sha256": sha(canonical(expected)), "complete_prepared_context": True,
                     "saved_roles": [row["role"] for row in expected_saved_context],
                     "wire_roles": [row["role"] for row in messages],
                     "wire_row_hashes": [sha(canonical(row)) for row in messages],
                     "expected_row_hashes": [sha(canonical(row)) for row in expected],
                     "users": [row["content"] for row in messages if row["role"] == "user"],
                     "last": messages[-1], "system_count": sum(row["role"] == "system" for row in messages)})
    stream = boundary.Stream()
    boundary.streams.append(stream)
    return stream


async def settle(app):
    from textual.worker import WorkerCancelled

    from litetui import hook_host

    await hook_host.drain_lifecycle(app)
    async def retire():
        for _ in range(8):
            workers = list(app.workers)
            outcomes = await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
            for worker, outcome in zip(workers, outcomes, strict=True):
                if isinstance(outcome, WorkerCancelled) and (worker.group != "chat" or worker.name == "deliver"):
                    # The hook admission worker deliberately yields its exclusive
                    # chat group to the actual stream; that stream must finish.
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
            await asyncio.sleep(0)
            if not list(app.workers):
                return
        raise AssertionError("Controlled owned history workers did not settle")
    await asyncio.wait_for(retire(), 8)


def common(app, *, session=None, catalog=None):
    return {"version": 1, "from_session_id": app.convo_id, "session_id": session or app.convo_id,
            "agent_id": app._agent_session.authority.agent_id, "backend": app.backend.name,
            "model": app.model_id, "scope": "preview" if session else "current", "catalog_snapshot": catalog}


def response_signature(page):
    return sha(canonical(page["fragments"]))


async def walk(management, descriptor, base):
    """Independently reconstruct every block and check forward/backward partition."""
    cursor = None
    chunks: dict[int, bytearray] = {}
    fields: dict[int, dict] = {}
    signatures = []
    pages = []
    for number in range(20000):
        request = {"type": PAGE, "id": f"history-page-{number}", **base,
                   "snapshot": descriptor["snapshot"], "cursor": cursor}
        if cursor is None:
            request["anchor"] = "head"
        page = await management(request)
        assert page["snapshot"] == descriptor["snapshot"]
        assert page["context_sha256"] == descriptor["context_sha256"]
        assert page["replay_sha256"] == descriptor["replay_sha256"]
        assert page["counts"] == descriptor["counts"]
        assert page["scope"] == base["scope"] and page["catalog_snapshot"] == base["catalog_snapshot"]
        assert len(page["fragments"]) <= 50 and page["at_start"] == (number == 0)
        assert (page["previous_cursor"] is None) == page["at_start"]
        assert (page["next_cursor"] is None) == page["at_end"]
        for fragment in page["fragments"]:
            index, part = fragment["block_index"], fragment["part"]
            body = fragment["text"].encode("utf-8")
            assert part["sha256"] == sha(body)
            chunk = chunks.setdefault(index, bytearray())
            assert part["utf8_start"] == len(chunk)
            assert part["utf8_end"] == len(chunk) + len(body) <= part["utf8_total"]
            stable = {key: value for key, value in fragment.items() if key not in {"text", "part"}}
            assert fields.setdefault(index, stable) == stable
            chunk.extend(body)
            if part["utf8_end"] == part["utf8_total"]:
                assert sha(chunk) == fragment["block_sha256"]
        signatures.append(response_signature(page))
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    else:
        raise AssertionError("History pager did not terminate")
    assert sorted(chunks) == list(range(descriptor["counts"]["blocks"]))
    cursor = None
    for number, expected in enumerate(reversed(signatures)):
        request = {"type": PAGE, "id": f"history-back-{number}", **base,
                   "snapshot": descriptor["snapshot"], "cursor": cursor}
        if cursor is None:
            request["anchor"] = "tail"
        page = await management(request)
        assert response_signature(page) == expected
        cursor = page["previous_cursor"]
    assert cursor is None
    blocks = [{**fields[index], "bytes": len(body), "text_sha256": sha(body),
               "text": bytes(body).decode("utf-8") if len(body) < 4096 else None}
              for index, body in sorted(chunks.items())]
    return {"pages": len(pages), "blocks": blocks, "page_signatures": signatures,
            "first_cursor": pages[0]["next_cursor"], "maximum_wire_bytes": max(
                len(json.dumps({"type": "response", "id": "history-page-00000", "ok": True, "result": page}).encode("utf-8"))
                for page in pages)}


async def serve(app):
    from litetui import gui_rpc, rpc, ttyguard

    global explicit, expected_saved_context
    boundary.ready = asyncio.Event()
    opened = False
    stale_used = False
    pending_open = None
    original_release = app.store.release

    def release():
        if getattr(app, "_gui_quitting", False):
            events.append({"effect": "lease-release", "pending": getattr(app, "_conversation_transition_pending", False),
                           "hook_alive": any(process.poll() is None for process in hook_processes),
                           "provider_active": provider_hydration_active})
            assert not getattr(app, "_conversation_transition_pending", False)
            assert not any(process.poll() is None for process in hook_processes)
            assert not provider_hydration_active
            if case in shutdown_cases:
                assert all(worker.is_finished for worker in worker_handles)
        return original_release()

    async def management(command):
        nonlocal opened, stale_used
        commands.append(copy.deepcopy(command))
        if (driver == "rpc" and case == "one-shot-stale" and command["type"] == PAGE
                and command.get("cursor") is not None and not stale_used):
            app.settings.max_tokens_chat += 1
            stale_used = True
        if command["type"] == OPEN and case == "hooks-drift":
            path = root / "hooks.json"
            changed = json.loads(path.read_text(encoding="utf-8"))
            changed["hooks"][0]["argv"] = ["changed-after-preview"]
            path.write_text(json.dumps(changed), encoding="utf-8")
        result = await asyncio.create_task(gui_rpc.async_dispatch(app, command))
        operation = command["type"]
        if operation == PAGE and driver == "rpc" and case == "cancel-page":
            await asyncio.sleep(0.3)
        if driver == "rpc":
            if operation == SNAPSHOT and case == "descriptor-drift":
                result["agent_id"] = str(uuid.uuid4())
            if operation == PAGE and case == "fragment-drift" and result["fragments"]:
                result["fragments"][0]["text"] += "FORGED"
            if operation == PAGE and case == "span-drift" and result["fragments"]:
                result["fragments"][0]["part"]["utf8_start"] += 1
            if operation == PAGE and case == "order-drift" and len(result["fragments"]) > 1:
                result["fragments"][0], result["fragments"][1] = result["fragments"][1], result["fragments"][0]
            if operation == PAGE and case == "cursor-loop":
                result["next_cursor"] = command.get("cursor") or "invalid-loop"
            if opened and operation == SNAPSHOT and case == "open-readback-drift":
                result["context_sha256"] = "0" * 64
        if operation == OPEN:
            opened = result.get("status") == "connected"
            report["events_at_open"] = copy.deepcopy(events)
        wire = len(json.dumps({"type": "response", "id": command.get("id"), "ok": True, "result": result}).encode("utf-8"))
        if operation in {SNAPSHOT, PAGE, OPEN}:
            assert wire <= MAX_WIRE
            observations.append({"type": operation, "wire_bytes": wire, "snapshot": result.get("snapshot"),
                                 "status": result.get("status"), "counts": result.get("counts")})
        return result

    async def prompt():
        global explicit, expected_saved_context
        expected_saved_context = copy.deepcopy(app.conversation)
        explicit = True
        command = {"type": "prompt", "id": "history-explicit-" + phase, "message": EXPLICIT, "tool_profile": "strict"}
        commands.append(command)
        rpc._dispatch(app, command)
        await settle(app)
        ended = [row for row in boundary.records if row.get("type") == "turn_end"]
        assert len(ended) == 1, "The explicit stream must finish exactly once"

    async def respond_open(command):
        try:
            result = await management(command)
            rpc._respond(command.get("id"), ok=True, result=result)
            return result
        except Exception as exc:
            rpc._respond(command.get("id"), ok=False, error=str(exc))
            raise

    async def shutdown(eof=False):
        if eof:
            rpc._exit_after_engine(app)
        else:
            command = {"type": "shutdown", "id": "history-shutdown"}
            commands.append(command)
            rpc._dispatch(app, command)
        await asyncio.wait_for(asyncio.shield(app._conversation_shutdown_exit_task), 8)

    with ExitStack() as stack:
        stack.enter_context(patch.object(app.store, "release", release))
        if case in hook_cases:
            stack.enter_context(patch.object(ttyguard, "popen", hook_spawn))
            stack.enter_context(patch.object(ttyguard, "kill_tree", hook_kill))
        for obj, key in ((socket, "create_connection"), (socket, "getaddrinfo"),
                         (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, key, boundary.forbidden))
        try:
            async with asyncio.timeout(100), app.run_test(size=(100, 32)):
                await asyncio.wait_for(boundary.ready.wait(), 15)
                await settle(app)
                ready = next(row for row in boundary.records if row.get("type") == "ready")
                report["ready"] = ready
                report["cli_thinking_level"] = app._cli_thinking_level
                report["launch_arguments"] = sys.argv[1:]
                assert ready["launch_status"] == "ready"
                report["startup_bubbles"] = copy.deepcopy(bubbles)
                report["startup_reads"] = copy.deepcopy(reads)
                seed(app)
                await settle(app)
                if case == "lmstudio-open" and phase == "fresh":
                    await gui_rpc.async_dispatch(app, {"type": "gui.thinking.set", "level": "default"})
                    assert app._cli_thinking_level is None
                    report["thinking_choice_retired_launch"] = True
                if case in hook_cases:
                    os.environ["LITETUI_HOOKS"] = "on"
                if case == "held":
                    from litetui import codex_steering
                    entry = {"id": "held-history-input", "threadId": "old-thread", "turnId": "old-turn",
                             "conversationId": app.convo_id, "state": "queued", "revision": 1,
                             "item": {"content": "NEVER REPLAY SAVED HISTORY INPUT", "source": "queued",
                                      "tool_profile": "strict"}}
                    app._append({"role": "assistant", "content": "Held before saved-history read",
                                 "provider_metadata": {"steering": [entry]}})
                    codex_steering.restore_queue(app)
                    assert len(app._pending_input) == 1 and app._pending_input[0]["_restored_input_held"]
                (root / "history-identity.json").write_text(json.dumps({
                    "current": app.convo_id, "target": target, "agent": app._agent_session.authority.agent_id
                }), encoding="utf-8")
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "history-hello", "protocol_version": 1})
                before, initial_disk = identity(app), disk(app)
                report.update(before=before, initial_disk=initial_disk, target=target)
                if driver == "rpc":
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        command = json.loads(line)
                        if case in shutdown_cases and command["type"] == OPEN:
                            pending_open = asyncio.create_task(respond_open(command))
                            continue
                        if command["type"].startswith("gui."):
                            try:
                                result = await management(command)
                                rpc._respond(command.get("id"), ok=True, result=result)
                            except Exception as exc:  # noqa: BLE001 - actual RPC response boundary
                                rpc._respond(command.get("id"), ok=False, error=str(exc))
                        else:
                            commands.append(copy.deepcopy(command))
                            if command["type"] == "prompt":
                                expected_saved_context = copy.deepcopy(app.conversation)
                            explicit = command["type"] == "prompt" or explicit
                            rpc._dispatch(app, command)
                            if command["type"] == "shutdown":
                                if case in shutdown_cases:
                                    await asyncio.wait_for(asyncio.shield(app._conversation_shutdown_exit_task), 8)
                                    assert pending_open is not None
                                    await pending_open
                                break
                            await settle(app)
                    else:
                        if case in shutdown_cases and pending_open is not None:
                            await shutdown(eof=True)
                            await pending_open
                elif case == "source-budget":
                    report["seed_only"] = True
                else:
                    base = common(app)
                    if target:
                        catalog = await management({"type": "gui.conversations.page", "id": "history-catalog",
                                                    "session_id": app.convo_id, "cursor": None})
                        base = common(app, session=target, catalog=catalog["snapshot"])
                    descriptor = await management({"type": SNAPSHOT, "id": "history-snapshot", **base})
                    report["descriptor"] = descriptor
                    if not target:
                        assert descriptor["context_sha256"] == before["context_sha256"]
                    report["walk"] = await walk(management, descriptor, base)
                    report["inspection_unchanged"] = identity(app) == before and disk(app) == initial_disk
                    report["provider_calls_at_inspection"] = copy.deepcopy(provider_calls)
                    assert report["inspection_unchanged"] and not requests
                    if case == "held":
                        assert identity(app) == before and not requests
                    elif case == "restart-cursor":
                        if phase == "resume":
                            prior = json.loads((root / "history-fresh-report.json").read_text(encoding="utf-8"))
                            command = {"type": PAGE, "id": "history-old-lifetime", **base,
                                       "snapshot": descriptor["snapshot"], "cursor": prior["walk"]["first_cursor"]}
                            assert command["cursor"] is not None
                            try:
                                await management(command)
                            except ValueError as exc:
                                report["restart_cursor_refusal"] = str(exc)
                            else:
                                raise AssertionError("A cursor from the previous process lifetime was accepted")
                            await prompt()
                    elif case in {"stale", "cursor-tamper", "foreign", "policy-stale"}:
                        command = {"type": PAGE, "id": "history-refusal", **base,
                                   "snapshot": descriptor["snapshot"], "cursor": None, "anchor": "head"}
                        if case == "stale":
                            app.store.record_edit(1, {"role": "user", "content": "Changed after snapshot"})
                        elif case == "policy-stale":
                            app.tools_enabled = not app.tools_enabled
                        elif case == "foreign":
                            command["agent_id"] = str(uuid.uuid4())
                        else:
                            command["cursor"] = "tampered-cursor"
                            command.pop("anchor")
                        refused_before = (identity(app), disk(app))
                        try:
                            await management(command)
                        except ValueError as exc:
                            report["refusal"] = str(exc)
                        else:
                            raise AssertionError("Stale/foreign/cursor request unexpectedly accepted")
                        report["refusal_unchanged"] = refused_before == (identity(app), disk(app))
                    elif case in {"hidden-system", "hidden-provider"}:
                        index = 0 if case == "hidden-system" else 2
                        updated = dict(app.conversation[index])
                        if case == "hidden-system":
                            updated["content"] += " Hidden changed policy"
                        else:
                            updated["provider_metadata"] = {"provider": "codex", "app_server_thread_id": "hidden-new-thread"}
                        app.conversation[index] = updated
                        app.store.record_edit(index, updated)
                        fresh = await management({"type": SNAPSHOT, "id": "history-hidden", **common(app)})
                        report["hidden_after"] = fresh
                        report["hidden_walk"] = await walk(management, fresh, common(app))
                        assert fresh["context_sha256"] != descriptor["context_sha256"]
                    elif target:
                        command = {"type": OPEN, "id": "history-open", **{
                            key: value for key, value in base.items() if key != "scope"},
                            "history_snapshot": descriptor["snapshot"], "action_id": descriptor["open_action_id"]}
                        if case == "open-stale":
                            from litetui.conversation import ConversationRepository
                            repository = ConversationRepository(agent_session=app._agent_session)
                            repository.adopt(app._agent_session.conversation_directory(target) / "convo.jsonl", target)
                            repository.record_edit(1, {"role": "user", "content": "Target changed after preview"})
                            repository.release()
                        if case in shutdown_cases:
                            pending_open = asyncio.create_task(management(command))
                            if case in provider_closing_cases:
                                await asyncio.wait_for(provider_hydration_entered.wait(), 5)
                                exiting = asyncio.create_task(shutdown(eof=case.startswith("codex-eof-")))
                                while not getattr(app, "_conversation_transition_shutdown_requested", False):
                                    await asyncio.sleep(0)
                                for _ in range(3):
                                    await asyncio.sleep(0)
                                assert not pending_open.done() and not exiting.done()
                                assert provider_hydration_active
                                assert not any(row["effect"] in {"lease-release", "shutdown-ack"} for row in events)
                                events.append({"effect": "provider-shutdown-held"})
                                provider_hydration_release.set()
                                await exiting
                            else:
                                assert await asyncio.to_thread(hook_entered.wait, 5)
                                await shutdown(eof=case == "eof-during-open")
                            report["open"] = await pending_open
                        else:
                            report["open"] = await management(command)
                        if report["open"]["status"] == "connected":
                            await settle(app)
                            fresh = await management({"type": SNAPSHOT, "id": "history-after-open", **common(app)})
                            report["after_open"] = fresh
                            report["after_open_walk"] = await walk(management, fresh, common(app))
                            report["provider_calls_after_open"] = copy.deepcopy(provider_calls)
                            assert not provider_requests
                            assert fresh["context_sha256"] == sha(canonical(app.conversation))
                            await prompt()
                    else:
                        report["prompt_context_before"] = sha(canonical(app.conversation))
                        await prompt()
                if case in hook_cases:
                    assert not getattr(app, "_tool_approval_pending", None)
                    assert not any(row.get("type") == "tool_approval" for row in boundary.records)
                report.update(after=identity(app), final_disk=disk(app), bubbles=copy.deepcopy(bubbles))
        finally:
            await app.client.close()
            if case == "preview-readonly" and target_path is not None:
                # App teardown already released the owned session. Use the exact
                # fixture path captured while owned, not a new authority lookup.
                assert target_path.resolve().is_relative_to(root.resolve())
                assert target_path.name == "convo.jsonl" and target_path.parent.name == target
                if target_path.exists():
                    target_path.chmod(stat.S_IREAD | stat.S_IWRITE)


def main():
    import httpx
    from openai.resources.chat.completions import AsyncCompletions

    from litetui import (
        gpu_gate,
        harness,
        image_viewer,
        llm_backend,
        mcp_client,
        native_history,
        plugins,
        rpc,
        thinking_probe,
        ttyguard,
        update_check,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository
    from litetui.custom_backend import CustomBackend

    prepare_hooks()
    original_init, original_emit = LiteTUI.__init__, rpc.rpc_emit
    original_read = ConversationRepository.read
    original_capture = native_history.capture_file
    original_prepare = LiteTUI._request_messages
    original_user, original_assistant = LiteTUI._user_bubble, LiteTUI._assistant_bubble

    def constructed(app, *args, **kwargs):
        boundary.app_instance = app
        original_init(app, *args, **kwargs)

    def emit(row):
        boundary.records.append(row)
        if row.get("type") == "response" and row.get("id") == "history-shutdown":
            events.append({"effect": "shutdown-ack", "ok": row.get("ok"),
                           "pending": getattr(boundary.app_instance, "_conversation_transition_pending", False),
                           "hook_alive": any(process.poll() is None for process in hook_processes),
                           "provider_active": provider_hydration_active})
        original_emit(row)
        if row.get("type") == "ready" and boundary.ready is not None:
            boundary.ready.set()

    def registered(seat):
        seat.registered, seat.error = True, ""
        return True

    def read(path, **kwargs):
        reads.append({"path": str(path), "max_bytes": kwargs.get("max_bytes")})
        return original_read(path, **kwargs)

    def capture(path):
        reads.append({"path": str(path), "max_bytes": native_history.MAX_TRANSCRIPT, "kind": "bounded-capture"})
        return original_capture(path)

    def prepare_request(app):
        global prepared_context
        value = original_prepare(app)
        prepared_context = copy.deepcopy(value)
        return value

    def user(app, *args, **kwargs):
        bubbles.append({"role": "user", "explicit": explicit})
        return original_user(app, *args, **kwargs)

    def assistant(app, *args, **kwargs):
        bubbles.append({"role": "assistant", "explicit": explicit})
        return original_assistant(app, *args, **kwargs)

    def track_worker(original):
        def launched(app, *args, **kwargs):
            worker = original(app, *args, **kwargs)
            worker_handles.append(worker)
            return worker
        return launched

    def run(app, **kwargs):
        assert kwargs == {"headless": True}
        asyncio.run(serve(app))

    with ExitStack() as stack:
        for name in ("connect", "fetch_context_window", "_fetch_ctx_window", "_refresh_native_history", "_probe_thinking"):
            stack.enter_context(patch.object(LiteTUI, name, track_worker(getattr(LiteTUI, name))))
        if case.startswith("codex-"):
            from litetui.codex_app_server import AppServer
            stack.enter_context(patch.object(AppServer, "start", codex_start))
            stack.enter_context(patch.object(AppServer, "request", codex_request))
            stack.enter_context(patch.object(AppServer, "close", codex_close))
        for obj, key, replacement in (
            (platform, "node", lambda: "controlled-host"), (subprocess, "Popen", boundary.forbidden),
            (httpx.Client, "send", boundary.forbidden), (httpx.AsyncClient, "send", boundary.forbidden_async),
            (ttyguard, "run", boundary.command), (ttyguard, "popen", boundary.forbidden),
            (gpu_gate, "nvidia_smi", lambda: "fixture-nvidia-smi"),
            (plugins, "PLUGIN_LOAD_ORDER", ("litetui.plugins.core_tools", "litetui.plugins.self_compact")),
            (rpc, "start_rpc_reader", lambda _app: None), (rpc, "rpc_emit", emit),
            (harness.Seat, "register", registered), (update_check, "start_background_check", lambda _app: None),
            (image_viewer, "init_image_backend", lambda: None),
            (LiteTUI, "__init__", constructed), (LiteTUI, "run", run),
            (LiteTUI, "_request_messages", prepare_request),
            (LiteTUI, "_user_bubble", user), (LiteTUI, "_assistant_bubble", assistant),
            (ConversationRepository, "read", staticmethod(read)),
            (native_history, "capture_file", capture),
            (AsyncCompletions, "create", completions),
            (CustomBackend, "_get", boundary.custom_get), (llm_backend, "_http_json", lmstudio_models),
            (thinking_probe, "get_effective_levels", boundary.forbidden if case.startswith("lmstudio-")
             else lambda *_args, **_kwargs: None),
        ):
            stack.enter_context(patch.object(obj, key, replacement))
        for method in ("load", "connect", "reload_configs"):
            stack.enter_context(patch.object(mcp_client.MCPManager, method, boundary.forbidden))
        gpu_gate.is_rtx_5090.cache_clear()
        try:
            module_spec = importlib.util.spec_from_file_location("litetui._actual_history_cli", source / "src/litetui/cli.py")
            assert module_spec and module_spec.loader
            cli = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(cli)
            cli.main()
        finally:
            gpu_gate.is_rtx_5090.cache_clear()
            from litetui import app as app_module
            assert Path(app_module.__file__).resolve() == source / "src/litetui/app.py"
            report.update(source_app=app_module.__file__, requests=requests, commands=commands,
                          observations=observations, denied=boundary.denied, reads=reads, bubbles=bubbles, events=events,
                          provider_calls=provider_calls, provider_requests=provider_requests,
                          worker_settlement=[{"name": worker.name, "group": worker.group,
                                              "finished": worker.is_finished} for worker in worker_handles],
                          interactive_events=[row for row in boundary.records
                                              if row.get("type") in {"user_input_requested", "tool_approval_requested"}])
            (root / ("history-" + phase + "-report.json")).write_text(json.dumps(report), encoding="utf-8")
            (root / "history-commands.jsonl").write_text(
                "".join(json.dumps(command) + "\n" for command in commands), encoding="utf-8")
        assert not boundary.denied, boundary.denied
        assert all(stream.closed for stream in boundary.streams)


if __name__ == "__main__":
    main()
