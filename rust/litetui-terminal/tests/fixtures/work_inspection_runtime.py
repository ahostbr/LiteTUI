"""Actual owned Work inspection while one controlled ordinary chat is streaming."""
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
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_WORK_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
case = os.environ.get("RUST_WORK_CASE", "success")
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="work-inspection")
spec = importlib.util.spec_from_file_location("_work_inspection_support", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
release_stream = asyncio.Event()
streams: list[Any] = []
workers: list[Any] = []
report: dict[str, Any] = {"commands": [], "responses": [], "requests": [], "checks": [], "lease_release": [], "summaries": [], "wire": []}
PREFIX = "gui.work.native_"


def delta(content=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=None), finish_reason=finish)], usage=None)


class Stream:
    closed = False

    def __aiter__(self):
        async def chunks():
            yield delta("WORK CHAT BUSY")
            await release_stream.wait()
            yield delta("; controlled chat finished")
            yield delta(finish="stop")
        return chunks()

    async def close(self):
        self.closed = True


async def completions(resource, **request):
    app = history.boundary.app_instance
    assert resource._client is app.client and request["model"] == app.model_id
    if not request.get("stream"):
        assert release_stream.is_set(), "Work inspection cannot request a summary"
        report["summaries"].append(copy.deepcopy(request))
        return NS(choices=[NS(message=NS(content="Controlled completed-turn card title"))])
    assert len(report["requests"]) == 0, "Only one explicit controlled chat is allowed"
    report["requests"].append(copy.deepcopy(request))
    stream = Stream()
    streams.append(stream)
    history.boundary.streams.append(stream)
    return stream


def protected(app):
    from litetui import scheduler
    paths = [root / "settings.json", app.convo_dir / "settings.json", app._agent_session.memory_root / "settings.json",
             scheduler.jobs_path(root), app.convo_dir / "goal.json"]
    paths += list((root / "output/tasks").glob("*.log"))
    return {"documents": {str(p): p.read_bytes().hex() if p.exists() else None for p in paths},
            "authority": asdict(app._agent_session.authority), "settings": asdict(app.settings),
            "pending": copy.deepcopy(app._pending_input), "tasks": [t.to_row() for t in app.bg_tasks.values()],
            "handles": [id(x) for x in (app.backend, app.client, app.store, app._agent_session, app.settings)]}


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        background_input,
        gui_rpc,
        native_work,
        rpc,
        scheduler,
        store_identity,
        tasks,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository
    from litetui.goal_loop import GoalState, save_goal

    for name, module in (("app", app_module), ("native_work", native_work), ("gui_rpc", gui_rpc)):
        assert module.__file__ is not None
        assert Path(module.__file__).resolve() == source / ("src/litetui/" + name + ".py")
        report["source_" + name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input is True
    assert background_input.activation_pending(app) and not background_input.enabled(app)
    report["launch_arguments"] = sys.argv[1:]
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    seeded = False
    reader = None
    buffered = None
    actual_gui, actual_dispatch, actual_emit = gui_rpc.async_dispatch, rpc._dispatch, rpc.rpc_emit
    actual_exit, actual_release, actual_worker = app.exit, app.store.release, LiteTUI.run_worker

    async def management(owner, command):
        operation = command["type"]
        if not operation.startswith(PREFIX):
            return await actual_gui(owner, command)
        if operation in {PREFIX + "capture", PREFIX + "activate"}:
            assert background_input.activation_pending(app) and not app._chat_running()
            before = protected(app)
            result = await actual_gui(owner, command)
            assert protected(app) == before
            return result
        assert background_input.enabled(app) and not background_input.activation_pending(app)
        assert operation != PREFIX + "settled", "Inspection must not create an autonomous admission"
        before = protected(app)
        assert app._chat_running(), "Every Work request must run during the actual blocked chat worker"
        if case == "readback-drift" and operation == PREFIX + "pause_readback":
            app._gui_schedules_paused = False
        try:
            with patch.object(scheduler, "refresh", history.boundary.forbidden), patch.object(scheduler, "load", history.boundary.forbidden):
                result = await actual_gui(owner, command)
            report["checks"].append({"operation": operation, "busy": True, "unchanged": protected(app) == before})
            assert protected(app) == before, "Work changed saved jobs/goal/task output/settings/queue/authority"
            return result
        finally:
            if operation == PREFIX + "pause_readback":
                report["pause_readback_attempted"] = True
                report["pause_after"] = app._gui_schedules_paused
                report["after_work"] = protected(app)
                release_stream.set()

    def dispatch(owner, command):
        nonlocal buffered
        report["commands"].append(copy.deepcopy(command))
        if command["type"] == "shutdown":
            release_stream.set()
            if buffered is not None:
                actual_emit(buffered)
                buffered = None
        return actual_dispatch(owner, command)

    def emit(row):
        nonlocal buffered
        report["wire"].append(copy.deepcopy(row))
        if row.get("type") == "ready":
            assert row.get("work_lifecycle_version") == 1 and background_input.activation_pending(app)
            report["startup_held"] = True
        if row.get("type") == "response":
            report["responses"].append(copy.deepcopy(row))
            command: dict[str, Any] = next((c for c in report["commands"] if c.get("id") == row.get("id")), {})
            if command.get("type", "").startswith(PREFIX):
                assert len(json.dumps(row).encode()) <= 1024 * 1024
            if command.get("type") == PREFIX + "pause_apply" and row.get("ok") and case == "quit-after-pause":
                buffered = copy.deepcopy(row)
                report["pause_after"] = app._gui_schedules_paused
                report["after_work"] = protected(app)
                pending = root / "work-stage.pending.json"
                pending.write_text(json.dumps({"stage": "pause-response"}), encoding="utf-8")
                pending.replace(root / "work-stage.json")
                return None
            if command.get("type") == "shutdown":
                report["shutdown"] = copy.deepcopy(row)
        return actual_emit(row)

    def launched(owner, *args, **kwargs):
        worker = actual_worker(owner, *args, **kwargs)
        workers.append(worker)
        return worker

    def collect():
        if seeded:
            report.update(after=protected(app), history=copy.deepcopy(app.conversation),
                          durable=ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)[1],
                          questions=list(getattr(app, "_rpc_pending_asks", {})),
                          approvals=list(getattr(app, "_approval_waiters", {})),
                          authorities=list(getattr(app, "_approval_authority_records", {})),
                          lifecycle_active=background_input.enabled(app),
                          lifecycle_producers=len(background_input.bridge(app).producers),
                          lifecycle_inputs=len(background_input.bridge(app).inputs))

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            row = {"active_chat": [w.name for w in workers if w.group == "chat" and not w.is_finished],
                   "questions": report["questions"], "approvals": report["approvals"]}
            report["lease_release"].append(row)
            assert all(not value for value in row.values())
        return actual_release()

    with ExitStack() as stack:
        for owner, name, replacement in ((gui_rpc, "async_dispatch", management), (rpc, "_dispatch", dispatch),
                (rpc, "rpc_emit", emit), (app, "exit", exit_app), (app.store, "release", release), (LiteTUI, "run_worker", launched)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(110), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ("memory.md", "soul.md", "handoff.md"):
                    (app._agent_session.memory_root / name).write_text("", encoding="utf-8")
                app.conversation = [{"role": "system", "content": "Work process fixture system"},
                    {"role": "user", "content": "Preserve prior history"}, {"role": "assistant", "content": "Preserved answer"}]
                app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                app._materialise_convo()
                app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                for index in range(35):
                    task = tasks.Task(id=f"t-work-{index:02d}", tool="controlled", label=f"Completed task {index}",
                        convo_id=app.convo_id, started=100.0-index, ended=101.0, state="done", ok=True,
                        log="../../untrusted-display-path", owner_pid=os.getpid(), owner_instance=tasks._INSTANCE_ID)
                    app.bg_tasks[task.id] = task
                log = tasks.log_path(app.bg_tasks["t-work-00"], root)
                log.parent.mkdir(parents=True, exist_ok=True)
                log.write_bytes(b"omitted prefix\n" * 6000 + b"CANONICAL WORK TAIL")
                jobs = [scheduler.Job(prompt="every five minutes", schedule="*/5 * * * *", id="frequent"),
                    scheduler.Job(prompt="disabled remains visible", schedule="0 12 * * *", id="disabled", enabled=False),
                    scheduler.Job(prompt="malformed remains visible", schedule="broken", id="broken"),
                    scheduler.Job.loop(prompt="loop excluded from calendar", interval_minutes=15, owner_convo_id=app.convo_id)]
                scheduler.save(jobs, root)
                app.jobs[:] = jobs
                save_goal(app.convo_dir, GoalState("Inspect paused goal", id="goal-work", status="paused"))
                await history.settle(app)
                seeded = True
                report["before"] = protected(app)
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name="work-inspection-rpc-reader", daemon=True)
                reader.start()
                await asyncio.wait_for(exited.wait(), 90)
                collect()
        finally:
            release_stream.set()
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{"name": w.name, "group": w.group, "finished": w.is_finished} for w in workers],
                          streams=[{"closed": s.closed} for s in streams], denied=history.boundary.denied,
                          calls=history.boundary.calls, reader_retired=reader is None or not reader.is_alive())
            (root / "work-inspection-report.json").write_text(json.dumps(report), encoding="utf-8")
            assert report["reader_retired"], "Fixture reader did not retire"


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
