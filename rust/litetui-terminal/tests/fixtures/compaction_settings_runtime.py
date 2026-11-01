"""Actual owned compaction preferences and their next canonical consumer.

Only SDK/network/process/platform boundaries are controlled. The CLI, RPC reader,
settings service, owned stores, native preference operations, and compaction run
from the selected actual source. No inference is allowed during preference edits.
"""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import socket
import threading
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_COMPACT_SETTINGS_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="compaction-preferences")
spec = importlib.util.spec_from_file_location("_compaction_settings_support",
    Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
CASE = os.environ.get("RUST_COMPACT_SETTINGS_CASE", "success")
DRIVER = os.environ.get("RUST_COMPACT_SETTINGS_DRIVER", "self")
FIELD = os.environ.get("RUST_COMPACT_SETTINGS_FIELD", "compact_max_tokens")
VALUE = json.loads(os.environ.get("RUST_COMPACT_SETTINGS_VALUE", "768"))
PREFIX = "gui.settings.native_compact_"
FIELDS = ("compact_keep_recent", "compact_max_tokens", "compact_max_tool_iters",
          "compact_thinking_level", "wake_after_compact", "clear_screen_after_compact")
SYSTEM = "Independent compaction preference system 雪."
SUMMARY = "Saved compaction preferences reached the actual canonical consumer."
report: dict[str, Any] = {"sdk_requests": [], "commands": [], "responses": [], "snapshots": [],
                          "reviews": [], "saves": [], "clear_screen_calls": [], "lease_release": []}
streams: list[Any] = []
workers: list[Any] = []
consumer_allowed = False
compact_rounds = 0


def common(app):
    return {"version": 1, "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
            "backend": app.backend.name, "model": app.model_id}


def state(app):
    return {"identity": history.identity(app), "settings": asdict(app.settings),
            "authority": asdict(app._agent_session.authority), "history": copy.deepcopy(app.conversation),
            "tools": app.tools_enabled, "pending": copy.deepcopy(app._pending_input),
            "handles": [id(value) for value in (app.settings, app.backend, app.client, app.store,
                                               app._agent_session, app._launch_options)]}


def documents(app):
    paths = {"global": root / "settings.json", "conversation": app.convo_dir / "settings.json",
             "agent": app._agent_session.memory_root / "settings.json"}
    return {key: {"path": str(path), "raw": path.read_text(encoding="utf-8") if path.exists() else None}
            for key, path in paths.items()}


def delta(content=None, tools=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=tools),
                         finish_reason=finish)], usage=None)


class Stream:
    def __init__(self, kind, round_no):
        self.kind, self.round_no, self.closed = kind, round_no, False
        streams.append(self)
        history.boundary.streams.append(self)

    def __aiter__(self):
        async def chunks():
            if self.kind == "compact" and (CASE == "round-cap" or self.round_no < 3) and CASE != "consume-tools-off":
                tools = [NS(index=0, id="pref-tool-" + str(self.round_no),
                    function=NS(name="fixture_unknown_compaction_tool", arguments="{}"))]
                yield delta(tools=tools)
                yield delta(finish="tool_calls")
            else:
                yield delta(SUMMARY if self.kind == "compact" else "Genuine preference wake completed.")
                yield delta(finish="stop")
        return chunks()

    async def close(self):
        self.closed = True


async def completions(resource, **request):
    global compact_rounds
    assert consumer_allowed, "Editing preferences must not infer"
    from litetui.app import WAKE_AFTER_COMPACT

    app = history.boundary.app_instance
    assert resource._client is app.client and request["model"] == app._agent_session.authority.model
    kind = "card-summary" if not request.get("stream") else (
        "wake" if any(row.get("content") == WAKE_AFTER_COMPACT for row in request["messages"]) else "compact")
    report["sdk_requests"].append({"kind": kind, "request": copy.deepcopy(request), **common(app)})
    if kind == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled card title"))])
    if kind == "compact":
        compact_rounds += 1
    return Stream(kind, compact_rounds)


async def until(predicate, timeout=30):
    async with asyncio.timeout(timeout):
        while not (result := predicate()):
            await asyncio.sleep(0.01)
        return result


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        gui_rpc,
        native_compaction_settings,
        rpc,
        settings_runtime,
        settings_service,
        store_identity,
    )

    assert Path(app_module.__file__).resolve() == source / "src/litetui/app.py"
    assert Path(native_compaction_settings.__file__).resolve() == source / "src/litetui/native_compaction_settings.py"
    report["source_app"] = app_module.__file__
    report["source_preferences"] = native_compaction_settings.__file__
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    reader = None
    serial = 0
    seeded = False
    saved_once = False
    buffered = None
    delayed_once = False
    actual_dispatch, actual_gui, actual_emit = rpc._dispatch, gui_rpc.async_dispatch, rpc.rpc_emit
    actual_write, actual_apply = settings_service._write, settings_runtime.apply_saved_result
    actual_clear, actual_exit, actual_release = app._clear_screen, app.exit, app.store.release
    actual_worker = LiteTUI.run_worker
    actual_eof = rpc._exit_after_engine
    settings_class = type(app.settings)
    actual_setattr: Any = settings_class.__setattr__

    def selected_path():
        return (root if FIELD == "clear_screen_after_compact" else app.convo_dir) / "settings.json"

    def write(path, raw, **kwargs):
        if Path(path) != selected_path():
            return actual_write(path, raw, **kwargs)
        if CASE == "persistence-refused":
            raise OSError("Controlled refusal before writing preferences")
        if CASE == "silent":
            return settings_service._read(path)[1]
        if CASE == "torn":
            path.write_text('{"incomplete":', encoding="utf-8")
            raise OSError("Controlled torn preference publication")
        document = copy.deepcopy(raw)
        if CASE == "wrong-field":
            (document if FIELD == "clear_screen_after_compact" else document["execution"])["compact_keep_recent"] = 999
        if CASE == "drop-unknown":
            document.pop("fixture-unknown")
        revision = actual_write(path, document, **kwargs)
        if CASE == "write-then-error":
            raise OSError("Controlled lost preference write acknowledgement")
        return revision

    def apply(*args, **kwargs):
        if CASE == "runtime-raised":
            raise RuntimeError("Controlled runtime apply exception")
        return actual_apply(*args, **kwargs)

    def set_setting(settings, name, value):
        if CASE == "runtime-failed" and settings is app.settings and name == FIELD and value == VALUE:
            raise RuntimeError("Controlled runtime setting rejection")
        return actual_setattr(settings, name, value)

    async def management(owner, command):
        nonlocal saved_once
        if command["type"] == PREFIX + "save":
            if CASE == "stale" and not saved_once:
                path = selected_path()
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw["concurrent-revision"] = "preserve"
                path.write_text(json.dumps(raw), encoding="utf-8")
                report["stale_disk"] = history.disk(app)
            saved_once = True
            with patch.object(settings_service, "_write", write), \
                    patch.object(settings_runtime, "apply_saved_result", apply), \
                    patch.object(settings_class, "__setattr__", set_setting):
                result = await actual_gui(owner, command)
            report["after_save"] = state(app)
            report["after_save_disk"] = history.disk(app)
            report["documents_after_save"] = documents(app)
            return result
        return await actual_gui(owner, command)

    def release_buffer(cause):
        nonlocal buffered
        if buffered is not None:
            report["buffer_release"] = {"cause": cause, "operation_id": buffered["id"]}
            actual_emit(buffered)
            buffered = None

    def eof(owner):
        release_buffer("eof")
        return actual_eof(owner)

    def dispatch(owner, command):
        global consumer_allowed
        report["commands"].append(copy.deepcopy(command))
        if command["type"] == "shutdown":
            release_buffer("quit")
        if command["type"] == "gui.conversations.native_compact_apply":
            consumer_allowed = True
            history.explicit = True
            report["before_compact"] = state(app)
        return actual_dispatch(owner, command)

    def emit(row):
        nonlocal buffered, delayed_once
        if row.get("type") == "response":
            report["responses"].append(copy.deepcopy(row))
            command: dict[str, Any] = next((c for c in report["commands"] if c.get("id") == row.get("id")), {})
            if command.get("type", "").startswith(PREFIX):
                assert len(json.dumps(row, ensure_ascii=False).encode("utf-8")) <= 65536
                key = {PREFIX + "snapshot": "snapshots", PREFIX + "validate": "reviews",
                       PREFIX + "save": "saves"}[command["type"]]
                if row.get("ok"):
                    report[key].append(copy.deepcopy(row["result"]))
            delayed_type = PREFIX + ("save" if CASE == "delay-save" else "snapshot")
            if CASE in {"delay-save", "delay-snapshot"} and not delayed_once and row.get("ok") and command.get("type") == delayed_type:
                delayed_once = True
                buffered = copy.deepcopy(row)
                result = row["result"]
                stage = {"stage": "save-response" if CASE == "delay-save" else "snapshot-response",
                         "operation_id": row["id"], "action_id": result.get("action_id"),
                         "field": result.get("field"), "value": result.get("value"), "status": result.get("status")}
                pending = root / "compaction-settings-stage.pending.json"
                pending.write_text(json.dumps(stage), encoding="utf-8")
                pending.replace(root / "compaction-settings-stage.json")
                report["buffered_response"] = copy.deepcopy(row)
                return None
            if row.get("ok") and command.get("type") == "shutdown":
                report["shutdown"] = copy.deepcopy(row)
        return actual_emit(row)

    def clear_screen(*args, **kwargs):
        if seeded:
            assert consumer_allowed, "Saving clear-screen must not clear now"
            report["clear_screen_calls"].append(copy.deepcopy(kwargs))
        return actual_clear(*args, **kwargs)

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def collect():
        if seeded:
            report.update(after=state(app), final_disk=history.disk(app), documents=documents(app),
                          durable=ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1],
                          question_pending=list(getattr(app, "_rpc_pending_asks", {})),
                          approval_pending=list(getattr(app, "_approval_waiters", {})),
                          approval_authorities=list(getattr(app, "_approval_authority_records", {})))
            raw = app.convo_path.read_bytes()
            report["archive_prefix_preserved"] = raw.startswith(bytes.fromhex(report["archive_before"]))

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            row = {"active_chat": [w.name for w in workers if not w.is_finished and w.group == "chat"],
                   "questions": list(getattr(app, "_rpc_pending_asks", {})),
                   "approvals": list(getattr(app, "_approval_waiters", {}))}
            report["lease_release"].append(row)
            assert not row["active_chat"] and not row["questions"] and not row["approvals"]
        return actual_release()

    async def call(operation, *, check=True, **fields):
        nonlocal serial
        serial += 1
        command = {"type": operation, "id": "pref-" + str(serial), **fields}
        rpc._dispatch(app, command)
        response = await until(lambda: next((r for r in history.boundary.records
            if r.get("type") == "response" and r.get("id") == command["id"]), None))
        if check:
            assert response["ok"], response
        return response

    async def edit(field, value, *, save_it=True):
        snapshot = (await call(PREFIX + "snapshot", **common(app)))["result"]
        review_response = await call(PREFIX + "validate", check=False, **common(app),
            snapshot=snapshot["snapshot"], revisions=snapshot["revisions"], field=field, value=value)
        if not review_response["ok"] or not save_it:
            return review_response
        review = review_response["result"]
        if CASE == "superseded":
            await call(PREFIX + "validate", **common(app), snapshot=snapshot["snapshot"],
                       revisions=snapshot["revisions"], field=field, value=value)
        response = await call(PREFIX + "save", **{key: val for key, val in review.items() if key != "kind"})
        if CASE == "duplicate":
            report["duplicate"] = await call(PREFIX + "save",
                **{key: val for key, val in review.items() if key != "kind"})
        if response["result"]["status"] in {"saved", "partial"}:
            report["readback"] = (await call(PREFIX + "snapshot", **common(app)))["result"]
            report["gui_state"] = (await call("gui.state", include_conversations=False))["result"]
        return response

    async def compact():
        descriptor = (await call(history.SNAPSHOT, **history.common(app)))["result"]
        selected = {"history_snapshot": descriptor["snapshot"], "context_sha256": descriptor["context_sha256"]}
        snapshot = (await call("gui.conversations.native_compact_snapshot", **common(app), **selected))["result"]
        review = (await call("gui.conversations.native_compact_validate", **common(app), **selected,
                            snapshot=snapshot["snapshot"], hint="Keep the tested settings."))["result"]
        receipt = await call("gui.conversations.native_compact_apply",
                             **{key: value for key, value in review.items() if key != "kind"})
        report["compact_snapshot"], report["compact_review"], report["compact_apply"] = snapshot, review, receipt
        report["compact_settled"] = await call("gui.conversations.native_compact_settled", **common(app),
                                              action_id=review["action_id"], operation_id=receipt["id"])

    with ExitStack() as stack:
        for owner, name, replacement in ((gui_rpc, "async_dispatch", management), (rpc, "_dispatch", dispatch),
            (rpc, "rpc_emit", emit), (rpc, "_exit_after_engine", eof), (app, "_clear_screen", clear_screen), (app, "exit", exit_app),
            (app.store, "release", release), (LiteTUI, "run_worker", launched)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                            (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(95), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ("memory.md", "soul.md", "handoff.md"):
                    (app._agent_session.memory_root / name).write_text("", encoding="utf-8")
                app.conversation = [{"role": "system", "content": SYSTEM},
                    *[{"role": role, "content": f"Preference history {index} {role}"}
                      for index in range(4) for role in ("user", "assistant")]]
                app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                app._materialise_convo()
                app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                await history.settle(app)
                for path in (root / "settings.json", app.convo_dir / "settings.json"):
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["fixture-unknown"] = {"unicode": "unchanged 雪", "nested": [False, 7]}
                    path.write_text(json.dumps(raw), encoding="utf-8")
                if CASE == "absent":
                    (app.convo_dir / "settings.json").unlink()
                seeded = True
                report.update(before=state(app), initial_disk=history.disk(app), initial_documents=documents(app),
                              archive_before=app.convo_path.read_bytes().hex())
                if DRIVER == "rpc":
                    reader = threading.Thread(target=rpc._reader_loop, args=(app,),
                                              name="compaction-preferences-rpc-reader", daemon=True)
                    reader.start()
                    await asyncio.wait_for(exited.wait(), 75)
                else:
                    hello = (await call("gui.hello", protocol_version=1))["result"]
                    assert {PREFIX + op for op in ("snapshot", "validate", "save")} <= set(hello["operations"])
                    if CASE.startswith("consume") or CASE == "round-cap":
                        values = {"compact_keep_recent": 2, "compact_max_tokens": 768,
                                  "compact_max_tool_iters": 2 if CASE == "round-cap" else 3,
                                  "compact_thinking_level": "high", "wake_after_compact": CASE != "consume-no-wake",
                                  "clear_screen_after_compact": CASE != "consume-no-clear"}
                        for field, value in values.items():
                            result = await edit(field, value)
                            assert result["result"]["status"] == "saved", result
                            assert not report["sdk_requests"] and not report["clear_screen_calls"]
                        report["saved_preferences"] = copy.deepcopy(values)
                        await compact()
                    else:
                        report["result"] = await edit(FIELD, True if CASE == "invalid" else VALUE,
                                                       save_it=CASE != "cancel")
                    await call("shutdown")
                    await asyncio.wait_for(exited.wait(), 20)
                collect()
                report["wire_events"] = copy.deepcopy(history.boundary.records)
        finally:
            await app.client.close()
            report.update(workers=[{"name": w.name, "group": w.group, "finished": w.is_finished} for w in workers],
                          streams=[{"kind": stream.kind, "closed": stream.closed} for stream in streams],
                          denied=history.boundary.denied, calls=history.boundary.calls)
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
                assert not reader.is_alive()
            (root / "compaction-settings-report.json").write_text(json.dumps(report), encoding="utf-8")


history.completions = completions
history.serve = serve
history.prepare_hooks = lambda: None

if __name__ == "__main__":
    from litetui import plugins

    original_register = plugins.register_plugins

    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        return original_register(app, registry, order=("litetui.plugins.core_tools",
            "litetui.plugins.convo", "litetui.plugins.self_compact"), disabled=disabled)

    with patch.object(plugins, "register_plugins", controlled_register):
        history.main()
