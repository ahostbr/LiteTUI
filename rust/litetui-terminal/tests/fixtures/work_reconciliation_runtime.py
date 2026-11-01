"""Actual Codex/Work reconciliation; only external provider and OS boundaries controlled."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import socket
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_RECONCILIATION_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
phase = os.environ["RUST_RECONCILIATION_PHASE"]
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="codex-work-reconciliation")
spec = importlib.util.spec_from_file_location("_reconciliation_boundaries", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
report: dict[str, Any] = {"commands": [], "wire": [], "provider": [], "lease_release": [], "checks": []}
workers: list[Any] = []
streams: list[Any] = []
closed_streams: set[int] = set()
helpers: list[Any] = []
thread_id = "work-native-thread"


def stage(name, value):
    pending = root / (phase + "-" + name + ".pending.json")
    pending.write_text(json.dumps(value), encoding="utf-8")
    pending.replace(root / (phase + "-" + name + ".json"))


async def complete(server, turn, text):
    await server.events.put({"method": "item/agentMessage/delta", "params": {
        "threadId": thread_id, "turnId": turn, "itemId": turn + "-answer", "delta": text}})
    await server.events.put({"method": "turn/completed", "params": {
        "threadId": thread_id, "turn": {"id": turn, "status": "completed"}}})


def start_task(app, content):
    async def output():
        return content
    return app._start_background("bash", {"command": "controlled coroutine; no process"}, output())


async def create_uncertainty(server, turn):
    from litetui import background_input as work
    app = history.boundary.app_instance
    await asyncio.sleep(0.05)  # The real provider installs its exact steering worker.
    start_task(app, "ORIGINAL UNCERTAIN BACKGROUND RESULT")
    async with asyncio.timeout(15):
        while not any(i.state == "uncertain" for i in work.bridge(app).inputs.values()):
            await asyncio.sleep(0.01)
        await complete(server, turn, "Original foreground completed")
        while app._chat_running():
            await asyncio.sleep(0.01)
        await asyncio.gather(*(p.observer for p in work.bridge(app).producers.values()))
        entry = next(iter(work.bridge(app).inputs.values()))
        if entry.provider_lane is not None:
            await entry.provider_lane.observer
        assert entry.admission is None and entry.state == "uncertain"
        stage("uncertain", {"input_id": entry.ref["input_id"], "delivery_id": entry.item["_codex_entry"]["id"],
                            "instance": work.bridge(app).instance})


async def provider_request(server, method, params):
    from litetui import background_input as work
    from litetui import restored_input
    from litetui.model_transport import ProviderError
    app = history.boundary.app_instance
    report["provider"].append({"method": method, "params": copy.deepcopy(params)})
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        return {"data": [{"model": "gpt-6.1-sol", "displayName": "Controlled Codex",
            "defaultReasoningEffort": "high", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}]}
    if method in {"thread/start", "thread/resume"}:
        return {"thread": {"id": thread_id, "turns": []}}
    if method == "thread/read":
        assert params["threadId"] == thread_id
        records = restored_input._records(app.conversation)
        native = next((value[0] for value in records.values() if value[0].get("work_input_id")), None)
        return {"thread": {"id": thread_id, "turns": [] if native is None else [{"id": native["turnId"],
            "status": "completed", "items": [{"type": "userMessage", "clientId": native["id"],
                "id": "accepted-native-input", "content": [{"type": "inputText", "text": native["item"]["content"]}]}]}]}}
    if method == "turn/steer":
        assert len([r for r in report["provider"] if r["method"] == "turn/steer"]) == 1
        native = app._pending_input[0]["_codex_entry"]
        assert native["state"] == "sending" and native["work_input_id"] in work.bridge(app).inputs
        raise ProviderError("Controlled lost turn/steer acceptance reply")
    if method == "turn/start":
        count = sum(r["method"] == "turn/start" for r in report["provider"])
        turn = "work-turn-" + str(count)
        if phase != "recovered" and count == 1:
            helpers.append(asyncio.create_task(create_uncertainty(server, turn)))
        else:
            assert not work.prompt_refusal(app) or any(i.admission is not None for i in work.bridge(app).inputs.values())
            assert not work.bridge(app).pending_readbacks
            await complete(server, turn, "AFTER VERIFIED RECONCILIATION")
        return {"turn": {"id": turn}}
    raise AssertionError("Unexpected provider method: " + method)


async def serve(app):
    from litetui import background_input as work
    from litetui import codex_steering, gui_rpc, restored_input, rpc, store_identity
    from litetui.app import LiteTUI
    from litetui.codex_app_server import AppServerStream
    from litetui.conversation import ConversationRepository

    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input
    assert app.backend.name == "codex" and work.activation_pending(app)
    report["launch_arguments"] = sys.argv[1:]
    for name, module in (("background_input", work), ("codex_steering", codex_steering), ("gui_rpc", gui_rpc), ("restored_input", restored_input)):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == source / ("src/litetui/" + name + ".py")
        report["source_" + name] = module.__file__
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    reader = None
    seeded = False
    actual_dispatch, actual_gui, actual_emit = rpc._dispatch, gui_rpc.async_dispatch, rpc.rpc_emit
    actual_exit, actual_release, actual_worker = app.exit, app.store.release, LiteTUI.run_worker
    actual_stream_init, actual_stream_close = AppServerStream.__init__, AppServerStream.close

    async def management(owner, command):
        result = await actual_gui(owner, command)
        if command["type"] == "gui.restored_input.list" and "work_reconciliation" in command:
            value = work.bridge(app)
            assert value.pending_readbacks and work.prompt_refusal(app) and result["held"] == 0
            before = len([r for r in report["provider"] if r["method"] in {"turn/start", "turn/steer"}])
            try:
                work.reserve(app)
            except ValueError:
                pass
            else:
                raise AssertionError("Source admitted before selected readback publication")
            stage("readback-held", {"selection": command["work_reconciliation"], "provider_sends": before})
            async with asyncio.timeout(20):
                while not (root / (phase + "-release-readback")).exists():
                    await asyncio.sleep(0.01)
            assert before == len([r for r in report["provider"] if r["method"] in {"turn/start", "turn/steer"}])
            assert value.pending_readbacks and work.prompt_refusal(app)
            report["checks"].append("admission-held-through-independent-readback")
        return result

    def dispatch(owner, command):
        report["commands"].append(copy.deepcopy(command))
        return actual_dispatch(owner, command)

    def emit(row):
        report["wire"].append(copy.deepcopy(row))
        if row.get("type") == "ready":
            assert row.get("work_lifecycle_version") == 1 and work.activation_pending(app)
        command: dict[str, Any] = next((c for c in report["commands"] if c.get("id") == row.get("id")), {})
        if row.get("type") == "response" and row.get("ok"):
            if command.get("type") == "gui.restored_input.list" and "work_reconciliation" in command:
                assert work.bridge(app).pending_readbacks and work.prompt_refusal(app)
                report["checks"].append("admission-held-at-response-publication")
                result = actual_emit(row)
                app.call_later(start_task, app, "FRESH BACKGROUND RESULT AFTER RECONCILIATION")
                return result
            if command.get("type") == "shutdown":
                report["shutdown"] = copy.deepcopy(row)
        return actual_emit(row)

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def stream_init(stream, iterator):
        actual_stream_init(stream, iterator)
        streams.append(stream)

    async def stream_close(stream):
        await actual_stream_close(stream)
        closed_streams.add(id(stream))

    def collect():
        if not seeded:
            return
        value = work.bridge(app)
        report.update(session_id=app.convo_id, history=copy.deepcopy(app.conversation),
            durable=ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1],
            pending=len(app._pending_input), questions=list(getattr(app, "_rpc_pending_asks", {})),
            approvals=list(getattr(app, "_approval_waiters", {})), authorities=list(getattr(app, "_approval_authority_records", {})))
        if value:
            report["lifecycle"] = {"instance": value.instance, "closing": value.closing,
                "readbacks": len(value.pending_readbacks),
                "inputs": [{"ref": i.ref, "payload": i.payload, "epoch": i.epoch, "state": i.state,
                    "admission": i.admission, "recovered": i.recovered, "receipt": i.receipt,
                    "worker_retired": i.worker is None or i.worker.is_finished,
                    "observer_retired": i.observer is None or i.observer.done()} for i in value.inputs.values()],
                "producers_retired": all(p.phase == "retired" and (p.worker is None or p.worker.is_finished)
                    and (p.observer is None or p.observer.done()) for p in value.producers.values())}

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            active = [w.name for w in workers if not w.is_finished and w.group in {"chat", "tasks", "codex-steering"}]
            report["lease_release"].append({"active": active, "questions": report["questions"], "approvals": report["approvals"]})
            assert not active and not report["questions"] and not report["approvals"]
        return actual_release()

    with ExitStack() as stack:
        for owner, name, replacement in ((rpc, "_dispatch", dispatch), (gui_rpc, "async_dispatch", management),
                (rpc, "rpc_emit", emit), (app, "exit", exit_app), (app.store, "release", release),
                (LiteTUI, "run_worker", launched), (AppServerStream, "__init__", stream_init), (AppServerStream, "close", stream_close)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(110), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 20)
                await history.settle(app)
                if phase != "recovered":
                    for name in ("memory.md", "soul.md", "handoff.md"):
                        (app._agent_session.memory_root / name).write_text("", encoding="utf-8")
                    app.conversation = [{"role": "system", "content": "Controlled Work reconciliation system"}]
                    app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                    app._materialise_convo()
                    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                else:
                    assert len(app._pending_input) == 1 and restored_input.held(app._pending_input[0])
                seeded = True
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name="work-reconciliation-rpc-reader", daemon=True)
                reader.start()
                await asyncio.wait_for(exited.wait(), 90)
                collect()
        finally:
            if helpers:
                await asyncio.gather(*helpers)
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{"name": w.name, "group": w.group, "retired": w.is_finished} for w in workers],
                streams=[{"retired": id(s) in closed_streams} for s in streams], denied=history.boundary.denied,
                reader_retired=reader is None or not reader.is_alive(), helpers_retired=all(t.done() for t in helpers))
            (root / (phase + "-work-reconciliation-report.json")).write_text(json.dumps(report), encoding="utf-8")


history.codex_request = provider_request
history.serve = serve
history.prepare_hooks = lambda: None
if __name__ == "__main__":
    history.main()
