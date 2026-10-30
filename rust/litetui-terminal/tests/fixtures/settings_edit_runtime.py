"""Owned settings editor subprocess; source lifecycle with controlled external I/O.

Reuse the committed backend-switch external-boundary harness. This file replaces
only its test driver and transport observer, never the product settings helpers.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import socket
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

source = Path(os.environ["RUST_SETTINGS_EDIT_SOURCE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ["RUST_BACKEND_SWITCH_SOURCE"] = str(source)
os.environ["RUST_BACKEND_SWITCH_CASE"] = "success"
spec = importlib.util.spec_from_file_location(
    "_settings_external_boundaries", Path(__file__).with_name("backend_switch_runtime.py"))
assert spec and spec.loader
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
case = os.environ.get("RUST_SETTINGS_EDIT_CASE", "success")
driver = os.environ.get("RUST_SETTINGS_EDIT_DRIVER", "self")
phase = os.environ.get("RUST_SETTINGS_EDIT_PHASE", "fresh")
field = os.environ.get("RUST_SETTINGS_EDIT_FIELD", "max_tokens_chat")
value = int(os.environ.get("RUST_SETTINGS_EDIT_VALUE", "257"))
requests: list[dict] = []


async def inference(transport, *, purpose="turn", **request):
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled settings summary"))])
    assert purpose == "turn" and boundary.app_instance is not None
    app = boundary.app_instance
    assert transport.backend.name == app._agent_session.authority.backend
    assert request["model"] == app._agent_session.authority.model
    requests.append({"backend": transport.backend.name, "model": request["model"],
                     "max_tokens": request.get("max_tokens"),
                     "messages": request["messages"]})
    stream = boundary.Stream()
    boundary.streams.append(stream)
    return stream


def disk(app):
    """Only durable settings/transcript files; locks are left to actual CLI."""
    candidates = [root / "settings.json", app._agent_session.memory_root / "settings.json",
                  app.convo_dir / "settings.json", app.convo_path]
    return {str(path): path.read_text(encoding="utf-8") if path.exists() else None
            for path in candidates}


def identity(app):
    return {**boundary.identity(app), "tools_enabled": app.tools_enabled,
            "history_count": len(app.conversation),
            "visible_history_count": sum(message.get("role") != "system" for message in app.conversation),
            "max_tokens_chat": app.settings.max_tokens_chat,
            "max_tokens_tools": app.settings.max_tokens_tools}


async def serve(app):
    from litetui import (
        gui_rpc,
        hook_host,
        rpc,
        settings_runtime,
        settings_service,
        store_identity,
    )

    boundary.ready = asyncio.Event()
    report = {}
    initial_disk = None
    saved = False
    stale_used = False
    actual_write = settings_service._write
    actual_apply = settings_runtime.apply_saved_result
    settings_class = type(app.settings)
    actual_setattr = settings_class.__setattr__
    runtime_failed = False

    def write(path, raw):
        if Path(path) == app.convo_dir / "settings.json":
            if case == "persistence-refused":
                raise OSError("Controlled settings persistence refusal")
            if case == "write-then-error":
                actual_write(path, raw)
                raise OSError("Controlled lost write acknowledgement")
        return actual_write(path, raw)

    def apply(*args, **kwargs):
        if case == "runtime-raised":
            raise RuntimeError("Controlled runtime application failure")
        return actual_apply(*args, **kwargs)

    def set_setting(settings, name, requested):
        nonlocal runtime_failed
        if (case == "runtime-failed" and settings is app.settings and name == field
                and requested == value and not runtime_failed):
            runtime_failed = True
            raise RuntimeError("Controlled known setting application failure")
        return actual_setattr(settings, name, requested)

    def mutate_saved():
        path = app.convo_dir / "settings.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["execution"][field] = int(document["execution"][field]) + 17
        path.write_text(json.dumps(document), encoding="utf-8")

    async def management(command):
        nonlocal saved, stale_used
        operation = command["type"]
        if (operation == "gui.settings.native_save" and case in {"stale", "one-shot-stale"}
                and (case == "stale" or not stale_used)):
            mutate_saved()
            stale_used = True
        with patch.object(settings_service, "_write", write), \
                patch.object(settings_runtime, "apply_saved_result", apply), \
                patch.object(settings_class, "__setattr__", set_setting):
            result = await gui_rpc.async_dispatch(app, command)
        if operation == "gui.settings.native_save":
            saved = result.get("status") == "saved"
        if saved and case == "value-drift" and operation == "gui.settings.native_snapshot":
            result["fields"][field]["effective"] += 1
        if saved and case == "policy-drift" and operation == "gui.state":
            result["tool_policy"]["tools_enabled"] = not app.tools_enabled
        return result

    async def dispatch_prompt(text):
        rpc._dispatch(app, {"type": "prompt", "id": "settings-explicit-" + str(len(requests)),
                            "message": text, "tool_profile": "strict"})
        await hook_host.drain_lifecycle(app)
        await asyncio.wait_for(boundary.settle_workers(app), 8)

    with ExitStack() as stack:
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, boundary.forbidden))
        try:
            async with asyncio.timeout(40), app.run_test(size=(100, 32)):
                await asyncio.wait_for(boundary.ready.wait(), 12)
                await hook_host.drain_lifecycle(app)
                await asyncio.wait_for(boundary.settle_workers(app), 5)
                status = next(row for row in boundary.records if row.get("type") == "ready")
                assert status["launch_status"] == "ready", status
                if phase == "fresh" and case != "empty":
                    app.conversation = [{"role": "system", "content": "Settings fixture system"},
                                        {"role": "user", "content": "Preserved settings history"},
                                        {"role": "assistant", "content": "Retained answer"}]
                    app._prompt_store_identity = store_identity.capture(app)
                    app._store_injected = True
                    app._materialise_convo()
                    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity,
                                              store_injected=True)
                    await hook_host.drain_lifecycle(app)
                    await asyncio.wait_for(boundary.settle_workers(app), 5)
                initial_disk = disk(app)
                before = identity(app)
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "settings-hello", "protocol_version": 1})
                if driver == "rpc":
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        command = json.loads(line)
                        with (root / "settings-edit-commands.jsonl").open("a", encoding="utf-8") as log:
                            log.write(json.dumps(command) + "\n")
                        if command["type"].startswith("gui."):
                            try:
                                result = await management(command)
                                rpc._respond(command.get("id"), ok=True, result=result)
                            except Exception as exc:  # noqa: BLE001 - actual RPC error boundary
                                rpc._respond(command.get("id"), ok=False, error=str(exc))
                        else:
                            rpc._dispatch(app, command)
                            if command["type"] == "shutdown":
                                break
                            await hook_host.drain_lifecycle(app)
                            await asyncio.wait_for(boundary.settle_workers(app), 8)
                    report = {"before": before}
                else:
                    snapshot = await management({"type": "gui.settings.native_snapshot", "id": "budget-snapshot",
                                                 "version": 1, "session_id": app.convo_id})
                    report = {"before": before, "snapshot": snapshot,
                              "snapshot_unchanged": disk(app) == initial_disk}
                    if phase == "resume":
                        await dispatch_prompt("Explicit prompt after settings restart")
                    else:
                        command = {"type": "gui.settings.native_validate", "id": "budget-validate", "version": 1,
                                   "session_id": app.convo_id, "agent_id": before["agent_id"],
                                   "snapshot": snapshot["snapshot"], "revisions": snapshot["revisions"],
                                   "field": field, "value": True if case == "invalid" else value}
                        try:
                            review = await management(command)
                        except Exception as exc:  # noqa: BLE001 - record the actual validation boundary
                            report["validation_error"] = str(exc)
                            report["validate_unchanged"] = disk(app) == initial_disk and identity(app) == before
                        else:
                            report["review"] = review
                            report["validate_unchanged"] = disk(app) == initial_disk and identity(app) == before
                            if case != "validate-only":
                                command.update(type="gui.settings.native_save", id="budget-save", review=review["review"])
                                report["result"] = await management(command)
                                report["after_save"] = identity(app)
                                report["saved_document"] = json.loads((app.convo_dir / "settings.json").read_text(encoding="utf-8"))
                                if report["result"]["status"] == "saved":
                                    report["readback"] = await management({"type": "gui.settings.native_snapshot", "id": "budget-readback",
                                                                          "version": 1, "session_id": app.convo_id})
                                    await dispatch_prompt("Explicit prompt after settings save")
                                elif report["result"].get("close_required"):
                                    await dispatch_prompt("Must not dispatch after uncertain settings")
                                elif case in {"runtime-failed", "persistence-refused"}:
                                    await dispatch_prompt("Explicit prompt with unchanged effective budget")
                report.update(after=identity(app), final_disk=disk(app),
                              settings_path=str(app.convo_dir / "settings.json"),
                              agent_name=app._agent_session.authority.name)
        finally:
            await app.client.close()
    from litetui import app as app_module
    assert Path(app_module.__file__).resolve() == source.resolve() / "src/litetui/app.py"
    report.update(requests=requests, calls=boundary.calls, denied=boundary.denied,
                  initial_disk=initial_disk, source_app=app_module.__file__)
    (root / ("settings-edit-" + phase + "-report.json")).write_text(json.dumps(report), encoding="utf-8")


async def codex_start(server):
    boundary.calls.append({"effect": "controlled-native-start"})
    server.initialized = True


async def codex_request(_server, method, params):
    boundary.calls.append({"effect": "controlled-native-request", "method": method})
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        return {"data": [{"model": "switch-old", "hidden": False,
                          "defaultReasoningEffort": "medium",
                          "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]}]}
    return boundary.forbidden()


if __name__ == "__main__":
    from litetui.codex_app_server import AppServer
    with patch.object(boundary, "serve", serve), patch.object(boundary, "inference", inference), \
            patch.object(AppServer, "start", codex_start), patch.object(AppServer, "request", codex_request):
        boundary.main()
