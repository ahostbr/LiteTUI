"""Saved-row inspection through actual owned CLI/RPC, reusing controlled boundaries."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import socket
import stat
import sys
import uuid
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

# Importing this fixture does not run it. Its main owns the actual CLI, mounted
# Textual app, source-path assertion, SDK boundary proof and OS/network tripwires.
spec = importlib.util.spec_from_file_location("_inspection_history_support", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
case = os.environ.get("RUST_INSPECTION_CASE", "saved")
driver = os.environ.get("RUST_INSPECTION_DRIVER", "self")
CATALOG = "gui.conversations.page"
EXPLICIT = "Explicit original conversation prompt after saved-row inspection"
readonly_paths: list[Path] = []


def state(app):
    return {"identity": history.identity(app), "disk": history.disk(app),
            "settings": asdict(app.settings), "authority": asdict(app._agent_session.authority),
            "objects": [id(value) for value in (app.conversation, app.store, app.settings,
                        app.backend, app.client, app._agent_session, app._launch_options, app._pending_input)],
            "workers": [id(worker) for worker in history.worker_handles],
            "hook_conversation": getattr(app, "_hook_conversation_id", None)}


def owned_fixture_path(path: Path) -> Path:
    path = path.resolve()
    assert path.is_relative_to(history.root.resolve()) and path.name == "convo.jsonl"
    assert str(uuid.UUID(path.parent.name)) == path.parent.name
    return path


def seed(app):
    from litetui import codex_steering, store_identity
    from litetui.conversation import ConversationRepository

    pending = case.startswith("pending")
    if not pending:
        app.conversation = [
            {"role": "system", "content": "ACTIVE SYSTEM CONTEXT: preserve this exact conversation"},
            {"role": "user", "content": "ACTIVE USER: original conversation only"},
            {"role": "assistant", "content": "ACTIVE ASSISTANT: retaining a completed tool call",
             "tool_calls": [{"id": "active-tool", "type": "function",
                             "function": {"name": "fixture_read", "arguments": "{}"}}]},
            {"role": "tool", "content": "ACTIVE TOOL RESULT: no target context", "tool_call_id": "active-tool"},
            {"role": "assistant", "content": "ACTIVE FINAL ANSWER"},
        ]
        app._prompt_store_identity = store_identity.capture(app)
        app._store_injected = True
        app._materialise_convo()
        app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
        os.utime(app.convo_path, (10000, 10000))
    if case == "held":
        entry = {"id": "inspection-held", "threadId": "active-thread", "turnId": "active-turn",
                 "conversationId": app.convo_id, "state": "queued", "revision": 1,
                 "item": {"content": "HELD ACTIVE INPUT: never replay", "source": "queued", "tool_profile": "strict"}}
        app._append({"role": "assistant", "content": "ACTIVE HELD LEDGER",
                     "provider_metadata": {"steering": [entry]}})
        codex_steering.restore_queue(app)
        assert len(app._pending_input) == 1 and app._pending_input[0]["_restored_input_held"]
        os.utime(app.convo_path, (10000, 10000))
    multiple = case in {"locate-boundaries", "reorder", "delete", "reorder-refresh", "delete-refresh", "stale-refresh"}
    shape = "many" if case == "large" else "rich" if case == "rich" else "astral" if case == "unicode" else "plain"
    targets = []
    for index in range(105 if multiple else 1):
        repository = ConversationRepository(agent_session=app._agent_session)
        repository.stage(str(uuid.uuid4()))
        repository.acquire()
        repository.pending = False
        repository.record_meta("historical-target-model", app._agent_session.authority.name,
                               app._agent_session.authority.agent_id)
        rows = history.saved_messages(shape)
        repository.record_snapshot(rows, store_identity=store_identity.capture(app), store_injected=True)
        repository.write_record({"type": "rename", "name": f"Inspected saved target {index} 雪"})
        if case == "rich":
            repository.record_card_summary(app._card_summary_key("Generic shown answer"), "Saved external summary")
        assert not repository.persist_error
        assert repository.convo_path is not None
        target, path = repository.convo_id, owned_fixture_path(repository.convo_path)
        repository.release()
        if case == "incompatible":
            (path.parent / "settings.json").write_text(json.dumps({
                "schema_version": 2, "model": "historical-target-model",
                "execution": {"tools_disabled": ["saved-only-tool-policy"]}}), encoding="utf-8")
        os.utime(path, (2000 - index, 2000 - index))
        if case == "readonly":
            path.chmod(stat.S_IREAD)
            readonly_paths.append(path)
        targets.append({"session_id": target, "path": str(path), "context_sha256": history.sha(history.canonical(rows))})
    return targets


async def serve(app):
    from litetui import gui_rpc, hook_host, rpc

    history.boundary.ready = asyncio.Event()
    targets = []
    read_checks = 0
    max_catalog_wire = 0

    async def read(command):
        nonlocal read_checks, max_catalog_wire
        assert command["type"] in {CATALOG, history.SNAPSHOT, history.PAGE, "gui.hello", "gui.state",
                                   "gui.themes.native_snapshot"}, f"Unexpected inspection read: {command['type']}"
        before = state(app)
        counts_before = (len(history.requests), len(history.provider_calls), len(history.provider_requests))
        history.commands.append(copy.deepcopy(command))
        try:
            # Arm after real startup has settled. These must never be needed by
            # any read, even when the selected saved row is incompatible to Open.
            with ExitStack() as tripwires:
                for name in ("connect", "_resume", "_resume_cli_conversation", "_refresh_native_history", "_materialise_convo"):
                    tripwires.enter_context(patch.object(app, name, history.boundary.forbidden))
                for name in ("invoke", "dispatch", "queue_lifecycle", "enter_conversation", "leave_conversation"):
                    tripwires.enter_context(patch.object(hook_host, name, history.boundary.forbidden))
                from litetui.conversation import ConversationRepository
                tripwires.enter_context(patch.object(ConversationRepository, "write_record", history.boundary.forbidden))
                result = await asyncio.create_task(gui_rpc.async_dispatch(app, command))
            wire = len(json.dumps({"type": "response", "id": command.get("id"), "ok": True, "result": result}).encode("utf-8"))
            limit = 256 * 1024 if command["type"] == CATALOG else history.MAX_WIRE
            assert wire <= limit
            if command["type"] == CATALOG:
                max_catalog_wire = max(max_catalog_wire, wire)
            history.observations.append({"type": command["type"], "wire_bytes": wire})
            return result
        finally:
            assert state(app) == before, "Inspection mutated active identity, handles, context, settings, workers or disk"
            assert (len(history.requests), len(history.provider_calls), len(history.provider_requests)) == counts_before
            read_checks += 1

    async def refusal(command):
        try:
            await read(command)
        except (ValueError, TypeError, OSError) as exc:
            history.report.setdefault("refusals", []).append(str(exc))
            return
        raise AssertionError("Invalid or stale inspection read was accepted")

    async def locate(target):
        page = await read({"type": CATALOG, "id": f"locate-{read_checks}",
                           "session_id": app.convo_id, "locate_session_id": target})
        assert page["located_session_id"] == target
        assert page["session_id"] == app.convo_id and page["agent_id"] == app._agent_session.authority.agent_id
        assert sum(row["session_id"] == target for row in page["entries"]) == 1
        assert page["offset"] % 50 == 0 and len(page["entries"]) <= 50
        return page

    async def inspect(target=None):
        catalog = await locate(target) if target else None
        base = history.common(app, session=target, catalog=catalog["snapshot"] if catalog else None)
        descriptor = await read({"type": history.SNAPSHOT, "id": f"snapshot-{read_checks}", **base})
        walk = await history.walk(read, descriptor, base)
        return {"catalog": catalog, "descriptor": descriptor, "walk": walk}

    async def prompt(command=None):
        history.report["before_prompt"] = history.identity(app)
        history.expected_saved_context = copy.deepcopy(app.conversation)
        history.explicit = True
        command = command or {"type": "prompt", "id": "inspection-explicit", "message": EXPLICIT, "tool_profile": "strict"}
        history.commands.append(command)
        rpc._dispatch(app, command)
        await history.settle(app)
        assert len([row for row in history.boundary.records if row.get("type") == "turn_end"]) == 1
        assert len(history.requests) == 1

    def edit_target(target):
        from litetui.conversation import ConversationRepository
        repository = ConversationRepository(agent_session=app._agent_session)
        repository.adopt(owned_fixture_path(Path(target["path"])), target["session_id"])
        repository.write_record({"type": "rename", "name": "Explicit synthetic reorder 雪"})
        assert not repository.persist_error
        repository.release()

    with ExitStack() as stack:
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(100), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                history.report["ready"] = next(row for row in history.boundary.records if row.get("type") == "ready")
                assert history.report["ready"]["history_projection_version"] == 1
                targets = seed(app)
                await history.settle(app)
                history.report.update(before=history.identity(app), targets=targets, active_context=copy.deepcopy(app.conversation))
                (history.root / "inspection-identity.json").write_text(json.dumps({
                    "current": app.convo_id, "target": targets[-1]["session_id"], "target_path": targets[-1]["path"],
                    "agent": app._agent_session.authority.agent_id}), encoding="utf-8")
                await read({"type": "gui.hello", "id": "inspection-hello", "protocol_version": 1})
                initial = state(app)
                if driver == "rpc":
                    locator_reads = 0
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        command = json.loads(line)
                        if history.route_work_handshake(app, command):
                            continue
                        if command["type"].startswith("gui."):
                            located_read = command["type"] == CATALOG and "locate_session_id" in command
                            if located_read:
                                locator_reads += 1
                                if locator_reads == 2 and case in {"reorder-refresh", "delete-refresh"}:
                                    if case == "delete-refresh":
                                        owned_fixture_path(Path(targets[-1]["path"])).unlink()
                                    else:
                                        edit_target(targets[-1])
                                        os.utime(owned_fixture_path(Path(targets[-1]["path"])), (12000, 12000))
                                    history.report["refresh_injection"] = case
                            try:
                                result = await read(command)
                                if located_read and locator_reads == 2 and case == "stale-refresh":
                                    # Deliberate external edit after the read completed: its
                                    # returned token must refuse the next snapshot once.
                                    edit_target(targets[-1])
                                    history.report["refresh_injection"] = case
                                stage = {CATALOG: "locate", history.SNAPSHOT: "snapshot", history.PAGE: "page"}.get(command["type"])
                                saved_read = located_read or command.get("scope") == "preview"
                                if stage and saved_read and case == "delay-" + stage:
                                    (history.root / "inspection-delayed-stage.json").write_text(json.dumps({
                                        "stage": stage, "session": command.get("locate_session_id", command.get("session_id"))}), encoding="utf-8")
                                    await asyncio.sleep(0.25)
                                rpc._respond(command.get("id"), ok=True, result=result)
                            except (ValueError, TypeError, OSError) as exc:
                                rpc._respond(command.get("id"), ok=False, error=str(exc))
                        elif command["type"] == "prompt":
                            assert command["message"] == EXPLICIT
                            await prompt(command)
                        else:
                            assert command["type"] == "shutdown"
                            history.commands.append(command)
                            rpc._dispatch(app, command)
                            break
                elif case == "locate-boundaries":
                    ordered = [app.convo_id] + [row["session_id"] for row in targets]
                    history.report["locations"] = []
                    for index in (49, 50, 99, 100, len(ordered) - 1):
                        page = await locate(ordered[index])
                        assert page["offset"] == index // 50 * 50
                        assert page["entries"][index % 50]["session_id"] == ordered[index]
                        history.report["locations"].append({"index": index, "offset": page["offset"], "target": ordered[index]})
                elif case == "invalid":
                    valid = targets[0]["session_id"]
                    base = {"type": CATALOG, "id": "invalid-locate", "session_id": app.convo_id}
                    for value in (None, "", "not-a-uuid", valid.upper(), str(uuid.uuid4()), 17):
                        await refusal({**base, "locate_session_id": value})
                    await refusal({**base, "locate_session_id": valid, "cursor": None})
                    await refusal({**base, "locate_session_id": valid, "cursor": "invalid"})
                elif case in {"reorder", "delete", "edit-after-locate", "delete-after-locate", "edit-after-snapshot", "policy-after-locate"}:
                    target = targets[-1]
                    prior = await inspect(target["session_id"])
                    history.report["inspection"] = prior
                    if case in {"delete", "delete-after-locate"}:
                        owned_fixture_path(Path(target["path"])).unlink()
                    elif case == "policy-after-locate":
                        app.settings.tools_disabled = [*app.settings.tools_disabled, "synthetic-policy-drift"]
                    else:
                        edit_target(target)
                        if case == "reorder":
                            os.utime(owned_fixture_path(Path(target["path"])), (12000, 12000))
                    base = history.common(app, session=target["session_id"], catalog=prior["catalog"]["snapshot"])
                    await refusal({"type": history.PAGE if case == "edit-after-snapshot" else history.SNAPSHOT,
                                   "id": "stale-inspection", **base, "snapshot": prior["descriptor"]["snapshot"], "anchor": "tail"})
                    if case in {"delete", "delete-after-locate"}:
                        await refusal({"type": CATALOG, "id": "deleted-locate", "session_id": app.convo_id,
                                       "locate_session_id": target["session_id"]})
                    else:
                        refreshed = await inspect(target["session_id"])
                        history.report["refreshed"] = refreshed
                        assert refreshed["catalog"]["snapshot"] != prior["catalog"]["snapshot"]
                        assert refreshed["descriptor"]["context_sha256"] == prior["descriptor"]["context_sha256"]
                        if case == "reorder":
                            assert prior["catalog"]["offset"] == 100 and refreshed["catalog"]["offset"] == 0
                else:
                    target = None if case == "current" else targets[0]["session_id"]
                    history.report["inspection"] = await inspect(target)
                    assert state(app) == initial
                    if case != "held":
                        history.report["before_prompt"] = history.identity(app)
                        await prompt()
                history.report.update(after=history.identity(app), read_checks=read_checks,
                                      maximum_catalog_wire=max_catalog_wire)
        finally:
            await app.client.close()
            for path in readonly_paths:
                # Teardown released AgentSession; use only captured synthetic paths.
                owned_fixture_path(path)
                if path.exists():
                    path.chmod(stat.S_IREAD | stat.S_IWRITE)


original_completions = history.completions


async def completions(resource, **request):
    if not history.explicit:
        history.boundary.forbidden()
    return await original_completions(resource, **request)


history.serve = serve
history.completions = completions
history.main()
