"""Owned engine management with real lifecycle code and controlled OS boundaries.

Only process/job/hardware/HTTP/inference/registration boundaries are synthetic.
CLI ownership, recipe inspection, admission, RPC, engine lifecycle and terminal
shutdown proof remain actual source code. No real engine or model is started.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import io
import json
import os
import socket
import struct
import sys
import threading
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

source = Path(os.environ["RUST_ENGINE_MANAGEMENT_SOURCE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ["RUST_BACKEND_SWITCH_SOURCE"] = str(source)
os.environ["RUST_BACKEND_SWITCH_CASE"] = "success"
spec = importlib.util.spec_from_file_location(
    "_engine_external_boundaries", Path(__file__).with_name("backend_switch_runtime.py"))
assert spec and spec.loader
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
case = os.environ.get("RUST_ENGINE_MANAGEMENT_CASE", "success")
driver = os.environ.get("RUST_ENGINE_MANAGEMENT_DRIVER", "self")
backend_name = sys.argv[sys.argv.index("--backend") + 1]
model_name = sys.argv[sys.argv.index("--model") + 1]
calls: list[dict] = []
requests: list[dict] = []
processes: list = []
jobs: dict[int, NS] = {}
ready = None
app_instance = None
spawn_entered = threading.Event()
spawn_release = threading.Event()
spawn_release.set()
if case in {"quit-during-start","attach-quit"}:
    spawn_release.clear()
stale_settings = None
readback_fault = False


class ControlledProcess:
    def __init__(self):
        self.pid = 2000000000 + len(processes)
        self.returncode = None
        self.loaded = True

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        assert timeout is None or timeout >= 0
        return self.returncode


def spawn(argv, **kwargs):
    assert app_instance is not None
    assert not getattr(app_instance, "_explicit_launch_load", False), "Management start bypassed resource confirmation"
    assert app_instance.backend._starting, "Real lifecycle claim is required before spawn"
    assert kwargs.get("shell") is None
    proc = ControlledProcess()
    processes.append(proc)
    config = None
    if "--config" in argv:
        config = json.loads(Path(argv[argv.index("--config") + 1]).read_text(encoding="utf-8"))
    calls.append({"effect": "spawn", "argv": list(argv), "config": config, "pid": proc.pid})
    spawn_entered.set()
    if case == "quit-during-start":
        (root / "engine-management-spawn-entered").write_text("controlled spawn waiting", encoding="utf-8")
    assert spawn_release.wait(15), "Cancellation fixture did not release controlled spawn"
    if case == "retained-start" and len(processes) == 1:
        proc.returncode = 1
        proc.loaded = False
    return proc


def job_create():
    handle = 9000 + len(jobs)
    jobs[handle] = NS(process=None, queries=0, terminated=0, closed=False)
    return handle


def job_assign(handle, pid):
    jobs[handle].process = next(proc for proc in processes if proc.pid == pid)
    return True


def job_terminate(handle):
    job = jobs[handle]
    assert not job.closed and job.process is not None
    job.terminated += 1
    job.process.returncode = 0
    job.process.loaded = False
    calls.append({"effect": "job-terminate", "handle": handle, "pid": job.process.pid})
    return True


def job_count(handle):
    job = jobs[handle]
    job.queries += 1
    calls.append({"effect": "job-count", "handle": handle, "query": job.queries})
    if case in {"retained-start", "retained-stop"} and job.queries == 1:
        return None
    return 0 if job.process.returncode is not None else 1


def job_close(handle):
    jobs[handle].closed = True
    calls.append({"effect": "job-close", "handle": handle})
    return True


class Response(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(json.dumps(body).encode("utf-8"))


def http(request, **_kwargs):
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    method = request.get_method() if isinstance(request, urllib.request.Request) else "GET"
    calls.append({"effect": "http", "url": url, "method": method})
    active = next((proc for proc in reversed(processes) if proc.returncode is None), None)
    attached = case in {"attached", "registered-foreign"} or (case.startswith("attach-") and (root/"engine-ready").exists())
    if active is None and not attached:
        raise urllib.error.URLError("Controlled engine is unavailable")
    if url.endswith("/health") and readback_fault and case == "source-health-drift":
        raise urllib.error.URLError("Controlled health changed before independent readback")
    if url.endswith("/health"):
        if case=="attach-quit" and attached and not spawn_entered.is_set():
            spawn_entered.set()
            (root/"attach-in-flight").write_text("bounded metadata read",encoding="utf-8")
            assert spawn_release.wait(15)
            calls.append({"effect":"attach-metadata-read-returned"})
        return Response({"loaded": active.loaded if active is not None else True})
    if url.endswith("/v1/unload") and method == "POST":
        assert active is not None, "Never unload an attached or registry-only engine"
        active.loaded = False
        return Response({"ok": True})
    if url.endswith("/v1/models"):
        selected = "wrong-engine-model" if (case == "model-drift" or (readback_fault and case == "source-model-drift")) and active is not None else model_name
        if case == "attach-wrong-model" or (case=="attach-readback-drift" and readback_fault):
            selected = "another-loaded-model"
        loaded = active.loaded if active is not None else True
        return Response({"data": [{"id": selected, "max_model_len": 8192,
                                   "meta": {"n_ctx": 8192}, "status": {"value": "loaded" if loaded else "unloaded"}}]})
    return boundary.forbidden()


def hardware(name, value):
    calls.append({"effect": "hardware", "name": name})
    return value


def gpu_command(argv, **kwargs):
    if argv == ["fixture-nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]:
        return NS(stdout="NVIDIA GeForce RTX 5090", returncode=0)
    return boundary.forbidden(argv, **kwargs)


async def inference(transport, *, purpose="turn", **request):
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled engine summary"))])
    assert purpose == "turn" and app_instance is not None
    assert transport.backend is app_instance.backend
    assert request["model"] == model_name == app_instance._agent_session.authority.model
    assert not getattr(app_instance, "_explicit_launch_load", False)
    requests.append({"backend": backend_name, "model": request["model"],
                     "messages": copy.deepcopy(request["messages"]),
                     "profile": app_instance._active_tool_profile,
                     "tools_enabled": app_instance.tools_enabled})
    stream = boundary.Stream()
    boundary.streams.append(stream)
    return stream


def files():
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file() and not path.name.endswith((".lock", ".lease")) and not path.name.startswith("engine-management-")}


def install():
    installation = root / "controlled-install"
    installation.mkdir(exist_ok=True)
    executable = installation / "ninfer-serve.exe"
    executable.write_bytes(b"controlled executable boundary only")
    (installation / "strata.exe").write_bytes(b"controlled executable boundary only")
    artifact = installation / "engine-model.ninfer"
    directory = json.dumps({"model_id": model_name, "components": {"text": {}}}).encode("utf-8")
    header = bytearray(32)
    header[:8] = b"NINFER\x00\x03"
    struct.pack_into("<Q", header, 8, len(directory))
    artifact.write_bytes(bytes(header) + directory)
    python = installation / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"controlled executable boundary only")
    server = installation / "serve/server.py"
    server.parent.mkdir(exist_ok=True)
    server.write_text("# never executed\n", encoding="utf-8")
    config = installation / "strata-engine-model.json"
    config.write_text(json.dumps({"exe": str(installation / "strata.exe"), "model_name": model_name,
                                 "port": 45128, "args": ["--max-context", "8192"]}), encoding="utf-8")
    settings_path = root / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings.update(ninfer_executable=str(executable), ninfer_artifact=str(artifact),
                    ninfer_host="http://127.0.0.1:45128" if case == "attached" or case.startswith("attach-") else "",
                    ninfer_max_context=8192, strata_root=str(installation), strata_config=str(config),
                    strata_host="", strata_max_context=None)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    return config


async def settle(app):
    from litetui import hook_host
    await hook_host.drain_lifecycle(app)
    await asyncio.wait_for(app.workers.wait_for_complete(), 8)
    await asyncio.sleep(0)


def state(app):
    return {"session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
            "backend": app.backend.name, "model": app.model_id,
            "history": copy.deepcopy(app.conversation),
            "pending": [{key: copy.deepcopy(value) for key, value in item.items() if key not in {"_codex_ledger", "bubble"}}
                        for item in app._pending_input],
            "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled}


def seed_held(app):
    from litetui import codex_steering
    assert not app._pending_input
    app._materialise_convo()
    entry = {"id": "held-engine-input", "threadId": "prior-native-thread", "turnId": "prior-native-turn",
             "conversationId": app.convo_id, "state": "queued", "revision": 2,
             "item": {"content": "NEVER AUTOMATICALLY REPLAY THIS INPUT", "source": "queued", "tool_profile": "strict"}}
    app._append({"role": "assistant", "content": "Saved before stopped engine recovery",
                 "provider_metadata": {"steering": [entry]}})
    codex_steering.restore_queue(app)
    assert len(app._pending_input) == 1 and app._pending_input[0]["_restored_input_held"] is True


async def serve(app):
    global ready, app_instance
    from litetui import gui_rpc, rpc
    app_instance = app
    ready = asyncio.Event()
    boundary.__dict__["ready"] = ready
    command_log: list[dict] = []
    results: list[dict] = []
    action_finished = False
    with ExitStack() as stack:
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, boundary.forbidden))
        try:
            async with asyncio.timeout(55), app.run_test(size=(100, 32)) as pilot:
                await asyncio.wait_for(ready.wait(), 15)
                await settle(app)
                initial = copy.deepcopy(next(row for row in boundary.records if row.get("type") == "ready"))
                if case == "held-input" and driver == "rpc":
                    seed_held(app)
                before = state(app)
                initial_disk = files()
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "engine-hello", "protocol_version": 1})

                async def dispatch(command):
                    nonlocal action_finished
                    global readback_fault
                    if command["type"] == "gui.engine.native_readback" and (case.startswith("source-") or case=="attach-readback-drift"):
                        readback_fault = True
                    command_log.append(copy.deepcopy(command))
                    with (root / "engine-management-commands.jsonl").open("a", encoding="utf-8") as output:
                        output.write(json.dumps(command) + "\n")
                    if command["type"].startswith("gui."):
                        result = await gui_rpc.async_dispatch(app, command)
                        if ((case=="config-only" and command["type"]=="gui.engine.native_config_snapshot")
                                or (command["type"]=="gui.engine.native_readback" and result.get("admission")=="management_only")):
                            from litetui import scheduler
                            old_history=copy.deepcopy(app.conversation)
                            old_requests=len(requests)
                            jobs_path=scheduler.jobs_path(root)
                            disk_before=jobs_path.read_bytes() if jobs_path.exists() else None
                            job=scheduler.Job(prompt="Blocked scheduler input",schedule="* * * * *")
                            assert app._fire_job(job,manual=False) is None
                            try:
                                app._fire_job(job,manual=True)
                            except ValueError:
                                pass
                            else:
                                raise AssertionError("Manual scheduled job crossed engine admission")
                            assert job.run_count==0 and disk_before==(jobs_path.read_bytes() if jobs_path.exists() else None)
                            with patch.object(app, "_rpc_emit") as refusal:
                                app._submit_text("Blocked typed engine prompt",alt_chord=False)
                            assert any(call.args[0]["type"]=="submit_refused" for call in refusal.call_args_list)
                            assert len(requests)==old_requests and app.conversation==old_history
                            calls.append({"effect":"verified-offline-refusal"})
                        results.append({"command": copy.deepcopy(command), "result": copy.deepcopy(result)})
                        with (root / "engine-management-results.jsonl").open("a", encoding="utf-8") as output:
                            output.write(json.dumps(results[-1]) + "\n")
                        if command["type"] == "gui.engine.native_action":
                            action_finished = True
                            if case == "one-shot-stale" and stale_settings is not None:
                                app.settings.ninfer_max_context, app.settings.strata_max_context = stale_settings
                            if case == "generic-action-error":
                                raise RuntimeError("Controlled missing receipt after actual engine action")
                        if case == "cancel-review" and command["type"] == "gui.engine.native_validate" and driver == "rpc":
                            await asyncio.sleep(0.3)
                        if action_finished and case == "policy-drift" and command["type"] == "gui.state":
                            result["tool_policy"]["tools_enabled"] = not result["tool_policy"]["tools_enabled"]
                        if action_finished and case == "history-drift" and command["type"] in {"gui.conversations.read", "gui.history.native_snapshot"}:
                            if "context_sha256" in result:
                                result["context_sha256"] = "0" * 64
                            else:
                                result["messages"].append({"role": "user", "content": "Controlled changed history"})
                        if case == "readback-drift" and command["type"] == "gui.engine.native_readback":
                            result["generation"] += 1
                        return result
                    rpc._dispatch(app, command)
                    if case in {"quit-during-start","attach-quit"} and command["type"] == "shutdown":
                        async with asyncio.timeout(3):
                            while not app._native_engine_state.cancel_start.is_set():
                                await asyncio.sleep(0.005)
                        calls.append({"effect": "shutdown-cancellation-requested"})
                        assert (app.backend._starting or case=="attach-quit") and not spawn_release.is_set()
                        spawn_release.set()
                    await settle(app)
                    return None

                if driver == "rpc":
                    in_flight = []

                    def publish(command, result):
                        from litetui import background_input
                        ticket = background_input.prepare_publication(app, command, result)
                        rpc._respond(command.get("id"), ok=True, result=result)
                        background_input.finish_publication(app, ticket)

                    async def action_reply(command):
                        try:
                            result = await dispatch(command)
                            publish(command, result)
                        except Exception as exc:  # noqa: BLE001 - actual wire response boundary
                            rpc._respond(command.get("id"), ok=False, error=str(exc))

                    while True:
                        line = await asyncio.to_thread(sys.stdin.readline)
                        if not line:
                            break
                        command = json.loads(line)
                        if case in {"quit-during-start","attach-quit"} and command["type"] == "gui.engine.native_action":
                            in_flight.append(asyncio.create_task(action_reply(command)))
                            continue
                        try:
                            result = await dispatch(command)
                            if command["type"].startswith("gui."):
                                publish(command, result)
                        except Exception as exc:  # noqa: BLE001 - same RPC isolation boundary
                            rpc._respond(command.get("id"), ok=False, error=str(exc))
                        if command["type"] == "shutdown":
                            break
                    if in_flight:
                        await asyncio.gather(*in_flight)
                        exit_task = getattr(app, "_native_engine_exit_task", None)
                        if exit_task is not None:
                            await exit_task
                else:
                    await drive(app, initial, dispatch, results)
                await settle(app)
                await pilot.pause()
                after = state(app)
                final_disk = files()
        finally:
            if app.client is not None:
                await app.client.close()
            # Real terminal proof executes while fake OS job/HTTP seams remain installed.
            if getattr(app.backend, "_owned", None) is not None:
                for job in jobs.values():
                    job.queries = max(job.queries, 1)
                await asyncio.to_thread(app.backend.shutdown)
        from litetui import app as imported_app
        assert Path(imported_app.__file__).resolve() == source.resolve() / "src/litetui/app.py"
        report = {"ready": initial, "before": before, "after": after,
                  "initial_disk": initial_disk, "final_disk": final_disk,
                  "calls": calls, "requests": requests, "denied": boundary.denied,
                  "commands": command_log, "results": results, "events": boundary.records,
                  "source_app": str(Path(imported_app.__file__).resolve()),
                  "processes": [{"pid": proc.pid, "returncode": proc.returncode} for proc in processes]}
        (root / "engine-management-report.json").write_text(json.dumps(report), encoding="utf-8")


async def barrier(app, results):
    from litetui import hook_host, rpc
    before = state(app)
    count = len(requests)
    rpc._dispatch(app, {"type": "prompt", "id": "blocked-direct-rpc", "message": "Blocked RPC prompt", "tool_profile": "strict"})
    app._submit_text("Blocked typed prompt", alt_chord=False)
    hook_host.start_prompt(app, {"content": "Blocked direct hook", "source": "queued", "tool_profile": "strict"})
    await hook_host.admit_prompt_origin(app, {"content": "Blocked async hook", "source": "queued", "tool_profile": "strict"})
    await settle(app)
    assert app.conversation == before["history"] and len(requests) == count
    queued = [{"content": "Keep first queued item exact", "source": "queued", "tool_profile": "strict"},
              {"content": "Keep second\nqueued item exact ", "source": "queued", "tool_profile": "strict"}]
    app._pending_input.extend(queued)
    expected = copy.deepcopy(app._pending_input)
    app._flush_pending_input()
    assert app._pending_input == expected, "Idle flush lost refused queued input"
    app._deliver_queued_input()
    assert app._pending_input == expected, "Round delivery lost refused queued input"
    await hook_host.queued_prompt(app)
    await settle(app)
    assert app._pending_input == expected, "Refused admission lost queued input"
    assert app.conversation == before["history"] and len(requests) == count
    hook_path = app.hook_config.project_path
    old_hook = hook_path.read_bytes() if hook_path.exists() else None
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(json.dumps({"version": 1, "hooks": [{"id": "must-not-run", "mode": "gate",
        "events": ["prompt_before"], "executable": sys.executable, "argv": ["must-not-run.py"]}]}), encoding="utf-8")
    old_hook_env = os.environ.pop("LITETUI_HOOKS", None)
    try:
        assert hook_host.snapshot(app).hooks
        await hook_host.queued_prompt(app)
        await settle(app)
        assert app._pending_input == expected, "Hooked queue delivery lost refused input"
        assert app.conversation == before["history"] and len(requests) == count
    finally:
        if old_hook_env is not None:
            os.environ["LITETUI_HOOKS"] = old_hook_env
        if old_hook is None:
            hook_path.unlink()
        else:
            hook_path.write_bytes(old_hook)
    del app._pending_input[-len(queued):]
    results.append({"barrier_preserved": True, "pending": expected})


async def drive(app, initial, dispatch, results):
    if initial["launch_status"] == "blocked":
        assert case == "blocked"
        assert not processes and not requests
        return
    common = {"version": 1, "session_id": app.convo_id,
              "agent_id": app._agent_session.authority.agent_id,
              "backend": backend_name, "desired_model": model_name,
              "management_instance": initial["management_instance"]}
    sequence = 0

    async def request(operation, **fields):
        nonlocal sequence
        sequence += 1
        return await dispatch({"type": "gui.engine.native_" + operation, "id": f"engine-source-{sequence}",
                               **common, **fields})

    async def inspect():
        return await request("inspect")

    async def review(snapshot, action):
        return await request("validate", snapshot=snapshot["snapshot"], generation=snapshot["generation"],
                             action=action, selection_id=snapshot["launch"]["selection_id"] if action == "start" else None,
                             handle_token=snapshot["ownership"]["handle_token"] if action == "stop" else None,
                             consent_scope="one_" + action)

    async def perform(reviewed):
        fields = {key: reviewed[key] for key in ("snapshot", "generation", "action", "selection_id",
                                                "handle_token", "consent_scope", "review", "action_id")}
        return await request("action", **fields)

    async def readback(receipt):
        return await request("readback", action_id=receipt["action_id"], review=receipt["review"], generation=receipt["generation"])

    if case == "barrier":
        await barrier(app, results)
        return
    if case == "held-input":
        held = await dispatch({"type": "gui.restored_input.list", "id": "engine-held-list", "session_id": app.convo_id})
        assert held["held"] == 1 and held["entries"][0]["can_cancel"]
        assert held["entries"][0]["can_continue"] is False
        assert not (await inspect())["actions"]["start"]["eligible"]
        entry = held["entries"][0]
        command = {"id": "engine-held-cancel", "session_id": app.convo_id,
                   "snapshot": held["snapshot"], "entry_id": entry["id"]}
        try:
            await dispatch({"type": "gui.restored_input.continue", **command})
        except ValueError:
            pass
        else:
            raise AssertionError("Management-only runtime continued held input")
        cancelled = await dispatch({"type": "gui.restored_input.cancel", **command})
        assert cancelled["cancelled"] == entry["id"] and not app._pending_input
        assert not requests and not processes
        from litetui.conversation import ConversationRepository
        saved = ConversationRepository.read(app.convo_path)[1]
        journals = [record for message in saved for record in message.get("provider_metadata", {}).get("steering", [])]
        assert journals[-1]["id"] == entry["id"] and journals[-1]["state"] == "denied"
    before_files, before_calls = files(), len(calls)
    snapshot = await inspect()
    assert not snapshot["readiness_checked"] and not snapshot["resources_checked"]
    assert files() == before_files
    assert calls[before_calls:] == [], "Pure engine inspection performed an external probe"
    if case in {"attached", "registered-foreign"}:
        assert snapshot["ownership"]["kind"] == "unowned"
        assert not snapshot["actions"]["stop"]["eligible"]
        assert not processes
        return
    assert initial["launch_status"] == "management" and snapshot["admission"] == "management_only"
    assert snapshot["launch"] is not None and snapshot["actions"]["start"]["eligible"], snapshot
    reviewed = await review(snapshot, "start")
    assert reviewed["summary"] == snapshot["launch"]
    assert files() == before_files and calls[before_calls:] == []
    if case == "cancel-review":
        assert not processes and not requests
        return
    if case == "stale":
        reviewed["generation"] += 1
        try:
            await perform(reviewed)
        except (ValueError, TypeError) as exc:
            results.append({"expected_error": str(exc)})
        else:
            raise AssertionError("Stale reviewed generation was accepted")
        assert not processes and (await inspect())["generation"] == snapshot["generation"]
        return
    if case == "plan-drift":
        app.settings.ninfer_max_context = 16384
        app.settings.strata_max_context = 16384
        try:
            outcome = await perform(reviewed)
        except (ValueError, TypeError) as exc:
            results.append({"expected_error": str(exc)})
        else:
            assert outcome["status"] == "refused"
        assert not processes
        return
    if case == "cancel-start":
        spawn_release.clear()
        task = asyncio.create_task(perform(reviewed))
        assert await asyncio.to_thread(spawn_entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0.05)
        assert app.backend._starting and not task.done(), "Canceled spawn released before joining"
        spawn_release.set()
        try:
            receipt = await task
            results.append({"cancel_receipt": receipt})
        except asyncio.CancelledError:
            results.append({"cancelled_after_join": True})
        assert not app.backend._starting
        assert app.backend._owned is not None, "Joined late spawn lost retained ownership"
        await barrier(app, results)
        snapshot = await inspect()
        receipt = await perform(await review(snapshot, "stop"))
        assert receipt["status"] == "stopped"
        await readback(receipt)
        return
    receipt = await perform(reviewed)
    assert receipt["action_id"] == reviewed["action_id"]
    assert receipt["generation"] == snapshot["generation"] + 1
    assert receipt["worker_settled"]
    if case in {"source-health-drift", "source-model-drift"}:
        try:
            await readback(receipt)
        except ValueError as exc:
            results.append({"expected_error": str(exc)})
        else:
            raise AssertionError("Changed source readiness returned a positive readback")
        assert app._native_engine_state.fatal
        await barrier(app, results)
        return
    actual = await readback(receipt)
    assert actual["action_id"] == receipt["action_id"] and actual["generation"] == receipt["generation"]
    if case.startswith("resource-") or case == "one-shot-stale":
        assert receipt["status"] == "refused" and receipt["admission"] == "management_only"
        assert not processes and not requests
        assert not any(row.get("type") == "model_load_requested" for row in boundary.records)
        await barrier(app, results)
        return
    if case == "model-drift":
        assert receipt["status"] in {"retained", "failed"} and receipt["admission"] != "prompt_ready"
        assert not requests
        return
    if case == "retained-start":
        assert receipt["status"] == "retained" and app.backend._owned is not None
        await barrier(app, results)
    else:
        assert receipt["status"] == "started" and receipt["admission"] == "prompt_ready"
        assert actual["connected"] and actual["loaded_model"] == model_name
        if case == "legacy-escape":
            before_legacy, retained = state(app), app.backend._owned
            generation, disk = app._native_engine_state.generation, files()
            blocked = ["gui.engine.start", "gui.engine.stop", "gui.models.reconnect", "gui.models.select",
                       "gui.models.load", "gui.models.unload", "gui.models.configure", "gui.context.set",
                       "gui.backend.set", "gui.conversations.open", "gui.conversations.create", "gui.settings.apply"]
            for operation in blocked:
                try:
                    await dispatch({"type": operation, "id": "legacy-" + operation, "name": "custom",
                                    "session_id": app.convo_id, "slug": model_name, "context": 4096})
                except ValueError as exc:
                    results.append({"expected_legacy_refusal": operation, "error": str(exc)})
                else:
                    raise AssertionError("Legacy mutation bypassed reviewed ownership: " + operation)
            for operation in ("set_model", "set_settings", "use_skill", "run_command"):
                ident = "legacy-" + operation
                await dispatch({"type": operation, "id": ident, "slug": model_name,
                                "command": "/engine stop", "patch": {"model": model_name}})
                reply = next(row for row in reversed(boundary.records) if row.get("id") == ident)
                assert reply["ok"] is False
            app._handle_command("/engine stop")
            await settle(app)
            assert app.backend._owned is retained and app._native_engine_state.generation == generation
            assert state(app) == before_legacy and files() == disk
        if case == "replay":
            try:
                await perform(reviewed)
            except (ValueError, TypeError) as exc:
                results.append({"expected_error": str(exc)})
            else:
                raise AssertionError("Consumed source action replayed")
            assert len(processes) == 1
        await dispatch({"type": "prompt", "id": "engine-explicit-prompt", "message": "Explicit prompt after engine start", "tool_profile": "strict"})
        assert len(requests) == 1
    snapshot = await inspect()
    assert snapshot["actions"]["stop"]["eligible"]
    receipt = await perform(await review(snapshot, "stop"))
    if case == "retained-stop":
        assert receipt["status"] == "retained" and receipt["shutdown"]["retained"]
        assert app.backend._owned is not None
        await barrier(app, results)
        snapshot = await inspect()
        assert not snapshot["actions"]["start"]["eligible"]
        receipt = await perform(await review(snapshot, "stop"))
    assert receipt["status"] == "stopped" and receipt["admission"] == "management_only"
    assert receipt["shutdown"]["main_exited"] and not receipt["shutdown"]["retained"]
    await readback(receipt)
    assert app.backend._owned is None
    await barrier(app, results)
    if case == "seed-held":
        seed_held(app)


def other_sibling(*_args):
    global stale_settings
    if (case == "one-shot-stale" and stale_settings is None and app_instance is not None
            and app_instance.backend._starting):
        stale_settings = (app_instance.settings.ninfer_max_context, app_instance.settings.strata_max_context)
        app_instance.settings.ninfer_max_context = 16384
        app_instance.settings.strata_max_context = 16384
        calls.append({"effect": "concurrent-settings-change"})
    return "controlled-local-sibling" if case == "resource-sibling" or case.startswith("peer-") else None

def main():
    import atexit

    from litetui import harness, jobkill, native_engine_endpoint, native_engine_peer, ninfer_engine, strata_engine
    install()
    if case == "registered-foreign" and backend_name == "ninfer":
        registry = ninfer_engine._config_path()
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text(json.dumps({"extraEndpoints": [{"kind": "ninfer", "baseUrl": "http://127.0.0.1:45128", "owner": "litetui", "pid": 2000000999}]}), encoding="utf-8")
    original_register = atexit.register

    def register(callback, *args, **kwargs):
        # Never leave a fake OS handle for a real callback after patch teardown.
        if callback is ninfer_engine.stop:
            calls.append({"effect": "engine-exit-callback-registered"})
            return callback
        return original_register(callback, *args, **kwargs)

    def launch_with_controlled_spawn(original):
        def launch(settings, **kwargs):
            assert "spawn" not in kwargs
            return original(settings, spawn=spawn, **kwargs)
        return launch

    peer_root=root/"simulated-local-registry"
    if case.startswith("peer-"):
        peer_root.mkdir()
        row={"agent_id":"22222222-2222-4222-8222-222222222222","cli":"litetui","name":"controlled-local-sibling",
             "backend":"lmstudio","session_pid":2000000444}
        (peer_root/(row["agent_id"]+".json")).write_text(json.dumps(row),encoding="utf-8")
    def birth(pid):
        if pid==os.getpid():return 123400
        assert pid==2000000444
        return 456701 if (root/"replace-peer").exists() else 456700
    with ExitStack() as stack:
        for obj, name, replacement in (
            (harness,"AGENTS_DIR",peer_root),
            (harness.router_record,"pid_is_live",lambda pid:pid==2000000444),
            (native_engine_peer,"_process_birth",birth),
            (native_engine_endpoint, "_open_pinned",lambda req,timeout:http(req,timeout=timeout)),
            (boundary, "serve", serve), (boundary, "inference", inference), (boundary, "command", gpu_command),
            (urllib.request, "urlopen", http), (atexit, "register", register),
            (jobkill, "create", job_create), (jobkill, "assign", job_assign),
            (jobkill, "terminate", job_terminate), (jobkill, "active_process_count", job_count),
            (jobkill, "close", job_close), (jobkill, "alive", boundary.forbidden), (ninfer_engine, "free_port", lambda: 45128),
            (ninfer_engine, "engine_process_alive", lambda: hardware("ninfer_process", False)),
            (ninfer_engine, "gpu_free_mib", lambda: hardware("ninfer_vram", 0 if case == "resource-memory" else 32768)),
            (strata_engine, "engine_process_alive", lambda: hardware("strata_process", case=="peer-competing-engine")),
            (strata_engine, "memory_free_bytes", lambda: hardware("ram", (128 * 1024**3, 256 * 1024**3))),
            (strata_engine, "gpu_memory_mib", lambda: hardware("strata_vram", (0 if case == "resource-memory" else 32768, 32768))),
            (harness, "other_live_litetui", other_sibling),
            (ninfer_engine, "start", launch_with_controlled_spawn(ninfer_engine.start)),
            (strata_engine, "start", launch_with_controlled_spawn(strata_engine.start)),
        ):
            stack.enter_context(patch.object(obj, name, replacement))
        boundary.global_settings_may_change = case == "config-only"
        boundary.main()


if __name__ == "__main__":
    main()
