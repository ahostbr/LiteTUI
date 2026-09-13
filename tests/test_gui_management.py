"""Behavioral contract for the structured desktop management interface."""
import json
import os
import subprocess
import sys
import time
from dataclasses import fields
from types import SimpleNamespace

import pytest

from litetui import settings


def test_protocol_refuses_unsupported_version():
    from litetui.gui_rpc import dispatch
    with pytest.raises(ValueError, match="protocol"):
        dispatch(SimpleNamespace(), {"type": "gui.hello", "protocol_version": 99})


def test_settings_inventory_is_complete_and_invalid_patch_does_not_mutate():
    from litetui.gui_rpc import dispatch
    app = SimpleNamespace(settings=settings.Settings(), thinking_level="medium")
    result = dispatch(app, {"type": "gui.settings.get"})
    assert {item["name"] for item in result["metadata"]} == {f.name for f in fields(settings.Settings)}
    with pytest.raises(ValueError, match="tool_iterations"):
        dispatch(app, {"type": "gui.settings.validate", "patch": {"tool_iterations": "nonsense"}})
    assert app.settings.tool_iterations == 48
    with pytest.raises(ValueError, match="unknown"):
        dispatch(app, {"type": "gui.settings.validate", "patch": {"invented": 2}})


def test_session_lease_blocks_other_process_and_recovers_after_exit(tmp_path):
    from litetui.shared_state import Lease, OwnershipError
    lease_path = tmp_path / "session.lease"
    code = "from litetui.shared_state import Lease; import sys; lease=Lease(sys.argv[1]); lease.acquire(); print('held', flush=True); sys.stdin.read()"
    env = {**os.environ, "PYTHONPATH": str(__import__('pathlib').Path(__file__).resolve().parents[1] / "src")}
    child = subprocess.Popen([sys.executable, "-c", code, str(lease_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(OwnershipError):
            Lease(lease_path).acquire()
        child.kill()
        child.wait(timeout=10)
        recovered = Lease(lease_path)
        deadline = time.monotonic() + 3
        while True:
            try:
                recovered.acquire()
                break
            except OwnershipError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        recovered.release()
        assert lease_path.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
async def test_legacy_rpc_prompt_refuses_foreign_session_then_recovers(tmp_path, monkeypatch):
    import asyncio
    from io import StringIO
    from pathlib import Path

    from litetui import app as app_mod
    from litetui import rpc

    monkeypatch.setattr(app_mod.LiteTUI, "connect", lambda self: None)
    wire = StringIO()
    monkeypatch.setattr(rpc, "_real_stdout", wire)
    app = app_mod.LiteTUI()
    app._stream = lambda: None
    child = None
    try:
        async with app.run_test(size=(100, 35)):
            lease = app.store.convo_dir / ".session.lease"
            code = ("from litetui.shared_state import Lease; import sys; "
                    "lease=Lease(sys.argv[1]); lease.acquire(); "
                    "print('held', flush=True); sys.stdin.readline(); lease.release()")
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
            child = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(lease),
                                                        stdin=asyncio.subprocess.PIPE,
                                                        stdout=asyncio.subprocess.PIPE, env=env)
            assert (await asyncio.wait_for(child.stdout.readline(), 10)).strip() == b"held"
            before = list(app.conversation)
            rpc._dispatch(app, {"type": "prompt", "id": "blocked", "message": "must not persist"})
            response = json.loads(wire.getvalue().splitlines()[-1])
            assert response["type"] == "response" and response["id"] == "blocked"
            assert response["ok"] is False and "owned by another process" in response["error"]
            assert app.conversation == before and not app.store.convo_path.exists()
            child.stdin.write(b"release\n")
            await child.stdin.drain()
            await asyncio.wait_for(child.wait(), 10)
            rpc._dispatch(app, {"type": "prompt", "id": "recovered", "message": "persist after release"})
            response = json.loads(wire.getvalue().splitlines()[-1])
            assert response["id"] == "recovered" and response["ok"] is True
            assert any(item.get("content") == "persist after release" for item in app.conversation)
            assert "persist after release" in app.store.convo_path.read_text(encoding="utf-8")
            assert "must not persist" not in app.store.convo_path.read_text(encoding="utf-8")
    finally:
        if child is not None and child.returncode is None:
            child.kill()
            await asyncio.wait_for(child.wait(), 10)


def test_compatibility_refuses_newer_data(tmp_path):
    from litetui.shared_state import check_data_version
    (tmp_path / ".litetui-data.json").write_text(json.dumps({"version": 999}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        check_data_version(tmp_path)


def test_explicit_missing_executable_does_not_fallback(tmp_path):
    from litetui.llm_backend import BackendError, llama_executable
    with pytest.raises(BackendError, match="selected"):
        llama_executable(settings.Settings(llama_executable=str(tmp_path / "missing.exe")))


@pytest.mark.asyncio
async def test_real_app_management_persists_history_settings_memory_and_jobs(tmp_path, monkeypatch):
    from litetui import app as app_mod
    from litetui import paths
    from litetui.gui_rpc import async_dispatch

    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")
    monkeypatch.setattr(app_mod.LiteTUI, "connect", lambda self: None)
    app = app_mod.LiteTUI()
    app.settings.mcp_enabled = False
    async with app.run_test(size=(100, 35)):
        hello = await async_dispatch(app, {"type": "gui.hello", "protocol_version": 1})
        assert "gui.host_tools.register" in hello["operations"]
        created = await async_dispatch(app, {"type": "gui.conversations.create"})
        session_id = created["session_id"]
        renamed = await async_dispatch(app, {"type": "gui.conversations.rename", "title": "Desktop history"})
        assert renamed["meta"]["name"] == "Desktop history"
        await async_dispatch(app, {"type": "gui.memory.write", "name": "soul.md", "text": "Persisted soul"})
        assert (tmp_path / ".convos" / session_id / "soul.md").read_text() == "Persisted soul"
        changed = await async_dispatch(app, {"type": "gui.settings.apply", "patch": {"max_tokens_chat": 1024}})
        assert changed["effective"]["max_tokens_chat"] == 1024
        jobs = await async_dispatch(app, {"type": "gui.jobs.create", "job": {"prompt": "Fixture job", "schedule": "0 9 * * *", "enabled": False}})
        assert jobs[0]["prompt"] == "Fixture job"
        state = await async_dispatch(app, {"type": "gui.state"})
        assert state["session_id"] == session_id
        assert state["ownership"]["writable"] is True
        extensions = await async_dispatch(app, {"type": "gui.extensions.list"})
        assert extensions["plugins"]
        hooks = await async_dispatch(app, {"type": "gui.hooks.get"})
        assert "tool_before" in hooks["events"]
        def refuse_save(_):
            raise OSError("fixture disk failure")
        monkeypatch.setattr(settings, "save", refuse_save)
        with pytest.raises(OSError, match="could not be saved"):
            await async_dispatch(app, {"type": "gui.settings.apply", "patch": {"max_tokens_chat": 2048}})
        assert app.settings.max_tokens_chat == 2048  # honest session-only outcome
        assert app._settings_persist_error == "fixture disk failure"


@pytest.mark.asyncio
async def test_host_tool_registration_goes_through_authority_and_disposes(tmp_path, monkeypatch):
    from litetui import app as app_mod
    from litetui import tool_policy
    from litetui.gui_rpc import async_dispatch

    monkeypatch.setattr(app_mod.LiteTUI, "connect", lambda self: None)
    app = app_mod.LiteTUI()
    app._system = lambda *a, **kw: None
    spec = {"type": "function", "function": {"name": "fixture_host", "description": "fixture", "parameters": {"type": "object", "properties": {}}}}
    await async_dispatch(app, {"type": "gui.host_tools.register", "plugin_id": "fixture", "tools": [spec]})
    assert app.plugins.policy_for("fixture_host") == tool_policy.MCP_UNKNOWN_POLICY
    app._active_tool_profile = tool_policy.SCHEDULED
    _text, ok = await app._execute_tool("fixture_host", {})
    assert ok is False
    assert not app._gui_host_pending  # denial never reached the host runner
    await async_dispatch(app, {"type": "gui.host_tools.unregister", "plugin_id": "fixture"})
    assert app.plugins.dispatch_for("fixture_host") is None
    with pytest.raises(ValueError, match="expired"):
        await async_dispatch(app, {"type": "gui.host_tools.result", "request_id": "old", "result": "ignored"})


def test_two_stores_cannot_write_one_session(tmp_path, monkeypatch):
    from litetui import paths
    from litetui.conversation import ConversationRepository
    from litetui.shared_state import OwnershipError
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")
    a, b = ConversationRepository(), ConversationRepository()
    a.stage("same-session")
    b.stage("same-session")
    a.acquire()
    with pytest.raises(OwnershipError):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


@pytest.mark.asyncio
async def test_correlated_host_tool_completes_and_rejects_late_result(monkeypatch):
    from litetui import app as app_mod
    from litetui import tool_policy
    from litetui.gui_rpc import async_dispatch, dispatch
    monkeypatch.setattr(app_mod.LiteTUI, "connect", lambda self: None)
    app = app_mod.LiteTUI()
    spec = {"type": "function", "function": {"name": "roundtrip", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}
    await async_dispatch(app, {"type": "gui.host_tools.register", "plugin_id": "fixture", "tools": [spec]})
    requests = []
    def emit(event):
        if event["type"] == "host_tool_requested":
            requests.append(event)
            dispatch(app, {"type": "gui.host_tools.result", "request_id": event["request_id"], "result": "host result"})
    monkeypatch.setattr(app, "_rpc_emit", emit)
    app._active_tool_profile = tool_policy.AUTONOMOUS
    async with app.run_test(size=(100, 35)):
        text, ok = await app._execute_tool("roundtrip", {"value": "fixture"})
        assert ok and text == "host result"
        assert requests[0]["arguments"] == {"value": "fixture"}
        with pytest.raises(ValueError, match="expired"):
            dispatch(app, {"type": "gui.host_tools.result", "request_id": requests[0]["request_id"], "result": "late"})


@pytest.mark.asyncio
async def test_model_load_confirmation_is_explicit_and_stale_ids_fail(monkeypatch):
    import asyncio

    from litetui import app as app_mod
    from litetui.gui_rpc import dispatch
    app = app_mod.LiteTUI(rpc=True)
    app._gui_rpc_enabled = True
    monkeypatch.setattr(app_mod.harness_mod, "other_live_litetui", lambda *a: "Terminal sibling")
    async def not_loaded(_):
        return (0, 0, False)
    monkeypatch.setattr(app.backend, "model_info", not_loaded)
    events = []
    monkeypatch.setattr(app, "_rpc_emit", events.append)
    waiting = asyncio.create_task(app._vram_gate_allows("another-model"))
    await asyncio.sleep(0)
    request = events[-1]
    assert request["type"] == "model_load_requested"
    assert not waiting.done()
    dispatch(app, {"type": "gui.models.confirm", "request_id": request["request_id"], "allow": False})
    assert await waiting is False
    with pytest.raises(ValueError, match="expired"):
        dispatch(app, {"type": "gui.models.confirm", "request_id": request["request_id"], "allow": True})


def test_two_scheduler_instances_deliver_a_slot_once(tmp_path, monkeypatch):
    from litetui import app as app_mod
    from litetui import scheduler
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    original = scheduler.Job(prompt="Once", schedule="* * * * *")
    scheduler.save([original], tmp_path)
    a_job, b_job = scheduler.load(tmp_path)[0], scheduler.load(tmp_path)[0]
    delivered = []
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda app, prompt: delivered.append(prompt))
    def fake(job):
        return SimpleNamespace(jobs=[job], convo_id="same", _chat_running=lambda: False,
                               _system=lambda *_: None, _user_bubble=lambda *_: None)
    app_mod.LiteTUI._fire_job(fake(a_job), a_job)
    app_mod.LiteTUI._fire_job(fake(b_job), b_job)
    assert len(delivered) == 1
    assert scheduler.load(tmp_path)[0].run_count == 1


def test_handshake_does_not_promote_deferred_settings(tmp_path, monkeypatch):
    from litetui.gui_rpc import dispatch
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    app = SimpleNamespace(settings=settings.Settings())
    dispatch(app, {"type": "gui.hello", "protocol_version": 1})
    original = app.settings.lm_host
    app.settings.lm_host = "http://127.0.0.1:9999"
    dispatch(app, {"type": "gui.hello", "protocol_version": 1})
    state = dispatch(app, {"type": "gui.settings.get"})
    assert state["effective"]["lm_host"] == original
    assert state["deferred"]["lm_host"] == "reconnect"


def test_lifecycle_inventory_includes_owned_background_work_only():
    from litetui.gui_rpc import active_work
    from litetui.tasks import Task
    ours = Task("ours", "subagent", "background", "session", 1, owner_pid=os.getpid())
    foreign = Task("foreign", "subagent", "sibling", "other-session", 1, owner_pid=987654321)
    app = SimpleNamespace(_chat_running=lambda: False, bg_tasks={t.id: t for t in (ours, foreign)}, workers=[])
    work = active_work(app)
    assert [entry["id"] for entry in work] == ["ours"]
    assert work[0]["cancellable"] is False


def test_screenshot_output_uses_data_root_without_running_capture(tmp_path, monkeypatch):
    from litetui import pccontrol_tool
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        output = __import__("pathlib").Path(argv[argv.index("-Output") + 1])
        output.write_bytes(b"fixture")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(pccontrol_tool.ttyguard, "run", run)
    result = pccontrol_tool.run({"action": "screenshot", "monitor": 2})
    assert str(tmp_path / "pccontrol" / "mon2.jpg") in result
    assert len(calls) == 1


def test_fresh_active_session_can_be_read_but_unknown_session_cannot(tmp_path, monkeypatch):
    from litetui import paths
    from litetui.gui_rpc import dispatch
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")
    app = SimpleNamespace(convo_id="fresh", conversation=[{"role": "system", "content": "fixture"}])
    result = dispatch(app, {"type": "gui.conversations.read", "session_id": "fresh"})
    assert result["messages"] == app.conversation and not result["read_only"]
    assert not paths.CONVO_DIR.exists()
    with pytest.raises(FileNotFoundError):
        dispatch(app, {"type": "gui.conversations.read", "session_id": "unknown"})


@pytest.mark.parametrize("enter_interrupts", [False, True])
def test_gui_midturn_submission_queues_or_interrupts_with_chosen_authority(tmp_path, monkeypatch, enter_interrupts):
    from litetui import app as app_mod
    from litetui.gui_rpc import dispatch
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    app = app_mod.LiteTUI()
    app.settings.enter_interrupts = enter_interrupts
    app._chat_running = lambda: True
    app._user_bubble = lambda *a, **kw: None
    app._scroll_down = lambda **kw: None
    app.watch_pending_image = lambda *_: None
    app.notify = lambda *a, **kw: None
    app.store.acquire = lambda: None
    dispatch(app, {"type": "gui.prompt.submit", "message": "queued", "behavior": "queue"})
    assert app._pending_input[-1]["content"] == "queued"
    assert app._pending_input[-1]["tool_profile"] == app.chosen_tool_profile
    assert not app._stop_requested
    dispatch(app, {"type": "gui.prompt.submit", "message": "interrupting", "behavior": "interrupt"})
    assert app._pending_input[0]["content"] == "interrupting"
    assert app._pending_input[0]["tool_profile"] == app.chosen_tool_profile
    assert app._stop_requested


def test_foreign_scheduler_does_not_disable_conversation_loop(tmp_path, monkeypatch):
    from litetui import app as app_mod
    from litetui import scheduler
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    job = scheduler.Job.loop(prompt="Owner only", interval_minutes=1, owner_convo_id="owner")
    job.next_run_at = "2000-01-01T00:00:00"
    scheduler.save([job], tmp_path)
    sibling = SimpleNamespace(jobs=[job], convo_id="sibling", _system=lambda *_: None)
    app_mod.LiteTUI._fire_job(sibling, job)
    assert scheduler.load(tmp_path)[0].enabled
    assert scheduler.load(tmp_path)[0].run_count == 0


def test_resume_after_refused_quit_restores_inbox_without_restarting_goals():
    from litetui.gui_rpc import dispatch
    app = SimpleNamespace(_gui_quitting=True, _gui_schedules_paused=True)
    assert dispatch(app, {"type": "gui.jobs.resume"}) == {"paused": False}
    assert app._gui_quitting is False


def test_usage_snapshot_normalizes_envelope_without_changing_legacy_event(monkeypatch):
    from litetui import app as app_mod
    from litetui import rpc
    events = []
    monkeypatch.setattr(rpc, "rpc_emit", events.append)
    app = SimpleNamespace(_rpc=True)
    event = {"type": "usage", "usage": {"context_tokens": 42}}
    app_mod.LiteTUI._rpc_emit(app, event)
    assert events == [event]
    assert app._gui_usage == {"context_tokens": 42}


def test_scheduler_refresh_observes_external_edit_addition_and_delete(tmp_path):
    from litetui import scheduler
    first = scheduler.Job(prompt="original", schedule="* * * * *")
    scheduler.save([first], tmp_path)
    held = scheduler.load(tmp_path)
    external = scheduler.load(tmp_path)
    external[0].prompt = "edited elsewhere"
    added = scheduler.Job(prompt="new", schedule="0 9 * * *")
    scheduler.save([*external, added], tmp_path)
    original_object = held[0]
    scheduler.refresh(held, tmp_path)
    assert held[0] is original_object and held[0].prompt == "edited elsewhere"
    assert held[1].id == added.id
    scheduler.save([added], tmp_path)
    scheduler.refresh(held, tmp_path)
    assert [job.id for job in held] == [added.id]


def test_compatibility_refuses_unknown_writer_protocol(tmp_path):
    from litetui.shared_state import check_data_version
    (tmp_path / ".litetui-data.json").write_text(json.dumps({"version": 1, "writer_protocol": 999}), encoding="utf-8")
    with pytest.raises(ValueError, match="protocol"):
        check_data_version(tmp_path)


@pytest.mark.asyncio
async def test_management_and_queued_submission_do_not_retag_active_turn(tmp_path, monkeypatch):
    from litetui import app as app_mod
    from litetui import rpc
    from litetui.gui_rpc import async_dispatch
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    app = app_mod.LiteTUI(rpc=True)
    app._gui_rpc_enabled = True
    app._gui_operation_id = "prompt-waiting-question"
    app._chat_running = lambda: True
    app._user_bubble = lambda *a, **kw: None
    app.watch_pending_image = lambda *_: None
    app.notify = lambda *a, **kw: None
    app.store.acquire = lambda: None
    events = []
    monkeypatch.setattr(rpc, "rpc_emit", events.append)
    await async_dispatch(app, {"type": "gui.images.clear", "id": "attachment-change"})
    await async_dispatch(app, {"type": "gui.prompt.submit", "id": "queued-message", "message": "Later", "behavior": "queue"})
    app._rpc_emit({"type": "turn_end", "stopReason": "complete"})
    assert events[-1]["operation_id"] == "prompt-waiting-question"
    assert app._pending_input[-1]["operation_id"] == "queued-message"
    app._materialise_convo = lambda: None
    app._append = lambda _: None
    assert app._deliver_queued_input()
    app._rpc_emit({"type": "turn_end", "stopReason": "complete"})
    assert events[-1]["operation_id"] == "prompt-waiting-question"


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [{"inference": {"max_tokens": "many"}}, {"inference": {"enable_thinking": "yes"}}, {"inference": {"reasoning_effort": "invented"}}, {"load": {"ngl": "bad"}}, {"apply_load": "false"}])
async def test_model_configuration_rejects_invalid_typed_values_before_save(patch, monkeypatch):
    from litetui.gui_rpc import async_dispatch
    saves = []
    monkeypatch.setattr(settings, "save", saves.append)
    app = SimpleNamespace(settings=settings.Settings(), model_id="fixture", _chat_running=lambda: False)
    with pytest.raises(ValueError):
        await async_dispatch(app, {"type": "gui.models.configure", **patch})
    assert not saves


def test_malformed_hook_repair_preserves_conflicting_external_edit(tmp_path):
    from litetui.gui_rpc import dispatch
    from litetui.lifecycle_hooks import HookConfig, HookError
    path = tmp_path / "global.json"
    path.write_bytes(b"{broken")
    app = SimpleNamespace(hook_config=HookConfig(path, tmp_path / "project.json"), hook_results={})
    result = dispatch(app, {"type": "gui.hooks.get"})
    assert result["scope_errors"]["global"]
    assert result["raw_documents"]["global"] == "{broken"
    path.write_bytes(b"{changed elsewhere")
    request = {"type": "gui.hooks.repair", "scope": "global", "text": '{"version":1,"hooks":[]}', "expected_bytes": result["raw_bytes"]["global"]}
    with pytest.raises(HookError, match="conflict"):
        dispatch(app, request)
    assert path.read_bytes() == b"{changed elsewhere"
    current = dispatch(app, {"type": "gui.hooks.get"})
    request["expected_bytes"] = current["raw_bytes"]["global"]
    assert dispatch(app, request) == {"hooks": [], "repaired": "global"}
    assert json.loads(path.read_text()) == {"version": 1, "hooks": []}


@pytest.mark.parametrize("delete", [False, True])
def test_scheduler_rechecks_persisted_candidate_after_external_change(tmp_path, monkeypatch, delete):
    from litetui import app as app_mod
    from litetui import scheduler
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    job = scheduler.Job(prompt="stale prompt", schedule="* * * * *")
    scheduler.save([job], tmp_path)
    held = scheduler.load(tmp_path)[0]
    external = scheduler.load(tmp_path)[0]
    external.enabled = False
    scheduler.save([] if delete else [external], tmp_path)
    delivered = []
    monkeypatch.setattr(app_mod.hook_host, "start_prompt", lambda *args: delivered.append(args))
    app = SimpleNamespace(jobs=[held], convo_id="session", _chat_running=lambda: False,
                          _system=lambda *_: None, _user_bubble=lambda *_: None)
    app_mod.LiteTUI._fire_job(app, held)
    assert not delivered


def test_two_process_mcp_edits_preserve_both_servers(tmp_path):
    script = '''
from pathlib import Path
import sys,time
from litetui import mcp_client
root=Path(sys.argv[1]); name=sys.argv[2]
original=mcp_client._load_doc
def held_read(path):
    value=original(path)
    if name=='first':
        print('read',flush=True)
        sys.stdin.readline()
    return value
mcp_client._load_doc=held_read
print('ready',flush=True)
error=mcp_client.MCPManager(root).add(name,{'command':'not-executed'},connect=False)
assert error is None,error
print('done',flush=True)
'''
    env = {**os.environ, "PYTHONPATH": str(__import__('pathlib').Path(__file__).resolve().parents[1] / "src")}
    first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "first"], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    second = None
    try:
        assert first.stdout.readline().strip() == "ready"
        assert first.stdout.readline().strip() == "read"
        second = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "second"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert second.stdout.readline().strip() == "ready"
        # The first has read but cannot write. An uncoordinated second writer
        # finishes now, then its server is lost when the first resumes.
        deadline = time.monotonic() + 0.5
        while second.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        first.stdin.write("release\n")
        first.stdin.flush()
        first.wait(timeout=5)
        second.wait(timeout=5)
        assert first.returncode == second.returncode == 0
        assert set(json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]) == {"first", "second"}
    finally:
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
