"""Actual owned CLI Work lifecycle, with only external provider/OS boundaries controlled."""
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
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_WORK_EXECUTION_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
case = os.environ.get("RUST_WORK_EXECUTION_CASE", "success")
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="work-execution")
spec = importlib.util.spec_from_file_location("_work_execution_support", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
streams: list[Any] = []
workers: list[Any] = []
helpers: list[Any] = []
second_started = asyncio.Event()
race_release = asyncio.Event()
report: dict[str, Any] = {"commands": [], "wire": [], "requests": [], "summaries": [], "lease_release": []}


def delta(content=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=None), finish_reason=finish)], usage=None)


class Stream:
    closed = False

    def __init__(self, ordinal):
        self.ordinal = ordinal

    def __aiter__(self):
        async def chunks():
            yield delta("CONTROLLED SOURCE WORK ANSWER" if self.ordinal == 1 else "SECOND SOURCE WORK ANSWER")
            if case == "overlap" and self.ordinal == 2:
                second_started.set()
                while not (root / "release-second").exists():
                    await asyncio.sleep(0.01)
            if case == "local-race":
                await race_release.wait()
            await asyncio.sleep(0)
            yield delta(finish="stop")
        return chunks()

    async def close(self):
        self.closed = True


async def completions(resource, **request):
    app = history.boundary.app_instance
    assert resource._client is app.client and request["model"] == app.model_id
    if not request.get("stream"):
        report["summaries"].append(copy.deepcopy(request))
        return NS(choices=[NS(message=NS(content="Controlled completed work title"))])
    assert len(report["requests"]) < (2 if case == "overlap" else 1), "Unexpected extra Work stream"
    report["requests"].append(copy.deepcopy(request))
    stream = Stream(len(report["requests"]))
    streams.append(stream)
    history.boundary.streams.append(stream)
    return stream


async def serve(app):
    from litetui import app as app_module
    from litetui import background_input, gui_rpc, rpc, store_identity
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository

    for name, module in (("app", app_module), ("background_input", background_input), ("gui_rpc", gui_rpc)):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == source / ("src/litetui/" + name + ".py")
        report["source_" + name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input is True
    assert background_input.activation_pending(app) and not background_input.enabled(app)
    report["launch_arguments"] = sys.argv[1:]
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    seeded = False
    started = 0
    reader = None
    buffering = False
    buffered: list[dict[str, Any]] = []
    actual_dispatch, actual_emit, actual_gui = rpc._dispatch, rpc.rpc_emit, gui_rpc.async_dispatch
    actual_exit, actual_release, actual_worker = app.exit, app.store.release, LiteTUI.run_worker

    def start_task():
        nonlocal started
        assert background_input.enabled(app) and started < (2 if case == "overlap" else 1)
        started += 1
        async def output():
            await asyncio.sleep(0)
            return "CANONICAL BACKGROUND TASK OUTPUT" if started == 1 else "SECOND CANONICAL TASK OUTPUT"
        app._start_background("bash", {"command": "controlled coroutine; no subprocess"}, output())

    async def management(owner, command):
        result = await actual_gui(owner, command)
        if case == "overlap" and command["type"] == "gui.work.native_settled" and started == 1:
            # The first exact receipt is withheld while a second genuine source
            # worker starts; the native foreground must survive this old read.
            start_task()
            await asyncio.wait_for(second_started.wait(), 15)
        return result

    def dispatch(owner, command):
        nonlocal buffering
        report["commands"].append(copy.deepcopy(command))
        if command["type"] == "shutdown":
            race_release.set()
            (root / "release-second").touch()
            buffering = False
            for row in buffered:
                actual_emit(row)
            buffered.clear()
        if command["type"] == "prompt" and case == "local-race":
            start_task()
            async def losing_prompt():
                while not any(i.admission is not None for i in background_input.bridge(app).inputs.values()):
                    await asyncio.sleep(0.001)
                actual_dispatch(owner, command)
            helpers.append(asyncio.create_task(losing_prompt()))
            return None
        return actual_dispatch(owner, command)

    def emit(row):
        nonlocal buffering
        if row.get("type") == "ready":
            assert row.get("work_lifecycle_version") == 1
            assert background_input.activation_pending(app) and not background_input.enabled(app)
            report["startup_held"] = True
        report["wire"].append(copy.deepcopy(row))
        if case == "quit-buffered" and row.get("type") == "work_admission":
            buffering = True
            pending = root / "buffered.pending.json"
            pending.write_text(json.dumps({"admission": row["admission"]["admission_id"]}), encoding="utf-8")
            pending.replace(root / "buffered.json")
        if buffering:
            buffered.append(copy.deepcopy(row))
            return None
        result = actual_emit(row)
        if row.get("type") == "response":
            command: dict[str, Any] = next((c for c in report["commands"] if c.get("id") == row.get("id")), {})
            if command.get("type") == "gui.work.native_activate" and row.get("ok") and case != "local-race":
                app.call_later(start_task)  # Activation ACK is already emitted.
            if command.get("type") == "prompt" and case == "local-race":
                assert row.get("ok") is False
                race_release.set()
            if command.get("type") == "shutdown":
                report["shutdown"] = copy.deepcopy(row)
        return result

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def collect():
        if not seeded:
            return
        value = background_input.bridge(app)
        report.update(history=copy.deepcopy(app.conversation),
            durable=ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1],
            questions=list(getattr(app, "_rpc_pending_asks", {})),
            approvals=list(getattr(app, "_approval_waiters", {})),
            authorities=list(getattr(app, "_approval_authority_records", {})),
            tasks=[task.to_row() for task in app.bg_tasks.values()],
            lifecycle_active=background_input.enabled(app))
        if value is not None:
            report["lifecycle"] = {"instance": value.instance, "closing": value.closing,
                "producers": [{"ref": p.ref, "phase": p.phase,
                    "worker_finished": p.worker is None or p.worker.is_finished,
                    "observer_done": p.observer is None or p.observer.done()} for p in value.producers.values()],
                "inputs": [{"ref": i.ref, "payload": i.payload, "admission": i.admission, "state": i.state,
                    "worker_finished": i.worker is None or i.worker.is_finished,
                    "observer_done": i.observer is None or i.observer.done(), "receipt": i.receipt} for i in value.inputs.values()]}
            report["ledger"] = json.loads(value.path.read_text(encoding="utf-8")) if value.path.exists() else None

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            active = [w.name for w in workers if w.group in {"chat", "tasks"} and not w.is_finished]
            row = {"active_workers": active, "questions": report["questions"], "approvals": report["approvals"]}
            report["lease_release"].append(row)
            assert all(not value for value in row.values())
        return actual_release()

    with ExitStack() as stack:
        for owner, name, replacement in ((gui_rpc, "async_dispatch", management), (rpc, "_dispatch", dispatch), (rpc, "rpc_emit", emit),
                (app, "exit", exit_app), (app.store, "release", release), (LiteTUI, "run_worker", launched)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        if case == "binding-fault":
            def broken_binding(_owner, _worker, _origin):
                raise ValueError("Controlled failure after exact worker creation")
            stack.enter_context(patch.object(background_input, "bind_stream", broken_binding))
        try:
            async with asyncio.timeout(110), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ("memory.md", "soul.md", "handoff.md"):
                    (app._agent_session.memory_root / name).write_text("", encoding="utf-8")
                app.conversation = [{"role": "system", "content": "Work execution fixture system"},
                    {"role": "user", "content": "Preserve existing input"}, {"role": "assistant", "content": "Preserved answer"}]
                app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                app._materialise_convo()
                app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                await history.settle(app)
                seeded = True
                report["before"] = copy.deepcopy(app.conversation)
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name="work-execution-rpc-reader", daemon=True)
                reader.start()
                await asyncio.wait_for(exited.wait(), 90)
                collect()
        finally:
            race_release.set()
            (root / "release-second").touch()
            if helpers:
                await asyncio.gather(*helpers, return_exceptions=True)
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{"name": w.name, "group": w.group, "finished": w.is_finished} for w in workers],
                streams=[{"closed": s.closed} for s in streams], denied=history.boundary.denied,
                calls=history.boundary.calls, reader_retired=reader is None or not reader.is_alive(),
                helper_retired=all(t.done() for t in helpers))
            (root / "work-execution-report.json").write_text(json.dumps(report), encoding="utf-8")
            assert report["reader_retired"], "Fixture RPC reader did not retire"


history.completions = completions
history.serve = serve
history.prepare_hooks = lambda: None

if __name__ == "__main__":
    from litetui import plugins
    original_register = plugins.register_plugins
    def controlled_register(app, registry, order=None, disabled=frozenset()):
        assert order is None
        return original_register(app, registry, order=("litetui.plugins.core_tools", "litetui.plugins.convo", "litetui.plugins.self_compact"), disabled=disabled)
    with patch.object(plugins, "register_plugins", controlled_register):
        history.main()
