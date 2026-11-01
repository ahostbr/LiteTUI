"""Canonical TaskStop through CLI; only controlled child OS/provider boundaries replaced."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
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

REAL_POPEN = subprocess.Popen
source = Path(os.environ["RUST_TASK_STOP_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ.update(RUST_HISTORY_SOURCE=str(source), RUST_HISTORY_CASE="task-stop")
spec = importlib.util.spec_from_file_location("_task_stop_support", Path(__file__).with_name("history_runtime.py"))
assert spec and spec.loader
history: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)
workers: list[Any] = []
streams: list[Any] = []
processes: list[Any] = []
report: dict[str, Any] = {"commands": [], "wire": [], "requests": [], "summaries": [], "lease_release": [], "kills": []}
CHILD = [sys.executable, "-u", "-c", "import time; print('CONTROLLED OWNED TASK',flush=True); time.sleep(60)"]


def delta(content=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, reasoning_content=None, tool_calls=None), finish_reason=finish)], usage=None)


class Stream:
    closed = False

    def __aiter__(self):
        async def chunks():
            yield delta("CONTROLLED STOP COMPLETION ANSWER")
            while not (root / "release-answer").exists():
                await asyncio.sleep(0.01)
            yield delta(finish="stop")
        return chunks()

    async def close(self):
        self.closed = True


async def completions(resource, **request):
    app = history.boundary.app_instance
    assert resource._client is app.client and request["model"] == app.model_id
    if not request.get("stream"):
        report["summaries"].append(copy.deepcopy(request))
        return NS(choices=[NS(message=NS(content="Controlled stopped task title"))])
    assert not report["requests"], "Unexpected second model stream"
    report["requests"].append(copy.deepcopy(request))
    stream = Stream()
    streams.append(stream)
    history.boundary.streams.append(stream)
    return stream


async def serve(app):
    from litetui import app as app_module
    from litetui import (
        background_input,
        gui_rpc,
        native_work,
        rpc,
        store_identity,
        tasks,
        ttyguard,
    )
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository
    from litetui.plugins import core_tools

    for name, module in (("app", app_module), ("background_input", background_input), ("gui_rpc", gui_rpc), ("native_work", native_work), ("tasks", tasks), ("core_tools", core_tools)):
        assert module.__file__ is not None
        expected = source / ("src/litetui/" + ("plugins/" if name == "core_tools" else "") + name + ".py")
        assert Path(module.__file__).resolve() == expected
        report["source_" + name] = module.__file__
    assert app._launch_options.work_lifecycle_version == 1 and app._hold_restored_input is True
    assert background_input.activation_pending(app) and not background_input.enabled(app)
    report["launch_arguments"] = sys.argv[1:]
    history.boundary.ready = asyncio.Event()
    exited = asyncio.Event()
    seeded = False
    task: Any = None
    reader = None
    actual_dispatch, actual_emit, actual_gui = rpc._dispatch, rpc.rpc_emit, gui_rpc.async_dispatch
    actual_exit, actual_release, actual_worker = app.exit, app.store.release, LiteTUI.run_worker

    def popen(argv, **kwargs):
        assert argv == CHILD and not processes
        assert kwargs["shell"] is False and kwargs["kill_on_close"] is True
        proc = REAL_POPEN(CHILD, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW)
        processes.append(proc)
        return proc

    def kill(pid, proc):
        assert len(processes) == 1 and proc is processes[0] and pid == proc.pid
        proc.kill()
        proc.wait(timeout=5)
        report["kills"].append({"pid": pid, "returncode": proc.returncode})
        return proc.poll() is not None

    def start_task():
        nonlocal task
        assert background_input.enabled(app) and task is None
        app._start_background("powershell", {"command": "CONTROLLED OWNED TASK: Python child; no external service"},
            asyncio.to_thread(core_tools._run_shell, CHILD, shell=False, timeout=75))
        task = list(app.bg_tasks.values())[-1]
        report["task_id"] = task.id

    async def management(owner, command):
        if command["type"] == "gui.work.native_snapshot":
            async with asyncio.timeout(15):
                while task is None or task.proc is None:
                    await asyncio.sleep(0.005)
            assert task.state == tasks.RUNNING and task.proc.poll() is None
        result = await actual_gui(owner, command)
        if command["type"] == "gui.work.native_stop_validate":
            assert task.state == tasks.RUNNING and task.proc.poll() is None and not report["kills"]
            report["review_without_effect"] = True
        if command["type"] == "gui.work.native_stop_settled":
            report["independent_readback"] = copy.deepcopy(result)
            (root / "readback-waiting").touch()
            async with asyncio.timeout(20):
                while not (root / "release-readback").exists():
                    await asyncio.sleep(0.01)
        return result

    def dispatch(owner, command):
        report["commands"].append(copy.deepcopy(command))
        if command["type"] == "shutdown":
            (root / "release-readback").touch()
            (root / "release-answer").touch()
        return actual_dispatch(owner, command)

    def emit(row):
        if row.get("type") == "ready":
            assert row.get("work_lifecycle_version") == 1
        report["wire"].append(copy.deepcopy(row))
        result = actual_emit(row)
        if row.get("type") == "response":
            command: dict[str, Any] = next((c for c in report["commands"] if c.get("id") == row.get("id")), {})
            if command.get("type") == "gui.work.native_activate" and row.get("ok"):
                app.call_later(start_task)
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
            questions=list(getattr(app, "_rpc_pending_asks", {})), approvals=list(getattr(app, "_approval_waiters", {})),
            authorities=list(getattr(app, "_approval_authority_records", {})),
            tasks=[t.to_row() for t in app.bg_tasks.values()],
            durable_tasks=json.loads((app.convo_path.parent / tasks.STORE).read_text(encoding="utf-8")))
        if value is not None:
            report["lifecycle"] = {"closing": value.closing,
                "producers": [{"ref": p.ref, "phase": p.phase, "worker_finished": p.worker is None or p.worker.is_finished,
                    "observer_done": p.observer is None or p.observer.done()} for p in value.producers.values()],
                "inputs": [{"ref": i.ref, "state": i.state, "admission": i.admission,
                    "worker_finished": i.worker is None or i.worker.is_finished,
                    "observer_done": i.observer is None or i.observer.done()} for i in value.inputs.values()]}

    def exit_app(*args, **kwargs):
        collect()
        exited.set()
        return actual_exit(*args, **kwargs)

    def release():
        if seeded:
            collect()
            row = {"active_workers": [w.name for w in workers if w.group in {"chat", "tasks", "cancel"} and not w.is_finished],
                   "questions": report["questions"], "approvals": report["approvals"],
                   "processes_alive": [p.pid for p in processes if p.poll() is None]}
            report["lease_release"].append(row)
            assert all(not value for value in row.values())
        return actual_release()

    with ExitStack() as stack:
        for owner, name, replacement in ((gui_rpc, "async_dispatch", management), (rpc, "_dispatch", dispatch), (rpc, "rpc_emit", emit),
                (app, "exit", exit_app), (app.store, "release", release), (LiteTUI, "run_worker", launched),
                (ttyguard, "popen", popen), (ttyguard, "kill_tree", kill)):
            stack.enter_context(patch.object(owner, name, replacement))
        for owner, name in ((socket, "create_connection"), (socket, "getaddrinfo"), (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(owner, name, history.boundary.forbidden))
        try:
            async with asyncio.timeout(110), app.run_test(size=(100, 32)):
                await asyncio.wait_for(history.boundary.ready.wait(), 15)
                await history.settle(app)
                for name in ("memory.md", "soul.md", "handoff.md"):
                    (app._agent_session.memory_root / name).write_text("", encoding="utf-8")
                app.conversation = [{"role": "system", "content": "Task Stop fixture system"},
                    {"role": "user", "content": "Preserve existing input"}, {"role": "assistant", "content": "Preserved answer"}]
                app._prompt_store_identity, app._store_injected = store_identity.capture(app), True
                app._materialise_convo()
                app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                await history.settle(app)
                seeded = True
                reader = threading.Thread(target=rpc._reader_loop, args=(app,), name="task-stop-rpc-reader", daemon=True)
                reader.start()
                await asyncio.wait_for(exited.wait(), 90)
                collect()
        finally:
            for proc in processes:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                    report["emergency_cleanup"] = True
            await app.client.close()
            if reader is not None:
                await asyncio.to_thread(reader.join, 2)
            report.update(workers=[{"name": w.name, "group": w.group, "finished": w.is_finished} for w in workers],
                streams=[{"closed": s.closed} for s in streams], denied=history.boundary.denied,
                reader_retired=reader is None or not reader.is_alive(),
                processes=[{"pid": p.pid, "returncode": p.returncode, "retired": tasks.process_retired(p),
                            "stdout_closed": p.stdout.closed, "stderr_closed": p.stderr.closed,
                            "evidence": getattr(p, "_litetui_retirement", None)} for p in processes])
            (root / "task-stop-report.json").write_text(json.dumps(report), encoding="utf-8")
            assert report["reader_retired"]


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
