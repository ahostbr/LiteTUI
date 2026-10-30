"""Controlled owned engine review, retention and source admission tests."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as _controlled_app

from litetui import (
    engine_review_plan,
    gui_rpc,
    hook_host,
    native_engine,
    ninfer_engine,
    strata_engine,
)
from litetui.launch_options import LaunchOptions
from litetui.llm_backend import BackendError
from litetui.strata_backend import StrataBackend

switch_app = _controlled_app


def install(settings, root):
    root.mkdir()
    python = strata_engine.server_python(root)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"synthetic interpreter")
    (root / "serve").mkdir()
    (root / "serve" / "server.py").write_text("# synthetic server", encoding="utf-8")
    executable = root / "engine.exe"
    executable.write_bytes(b"synthetic executable")
    artifact = root / "model.gguf"
    artifact.write_bytes(b"synthetic artifact")
    config = root / "strata-model.json"
    config.write_text(json.dumps({"exe": str(executable), "port": 45678, "model_name": "old-model",
                                 "args": ["--native", str(artifact), "--max-context", "8192"]}), encoding="utf-8")
    settings.strata_root, settings.strata_config = str(root), str(config)
    settings.strata_host = ""
    return config


@pytest_asyncio.fixture
async def engine_app(switch_app, monkeypatch, tmp_path):
    app = switch_app
    app._agent_session.update_execution(backend="strata", model="old-model", thinking_level="default")
    app.settings.backend = "strata"
    install(app.settings, tmp_path / "engine")
    app.backend = StrataBackend(app.settings)
    app.backend.vram_gate = app._vram_gate_allows
    app._launch_options = LaunchOptions(server_mode="connect", engine_management=True)
    app._launch_backend = "strata"
    app._cli_initial_backend = "strata"
    app._gui_connection_success = False
    app._native_engine_management_reason = "engine_unavailable"
    app._native_engine_state = native_engine.State(app.backend, session_id=app.convo_id, desired_model="old-model",
                                                          session=app._agent_session, agent_id=app._agent_session.authority.agent_id)
    app._native_history_worker = None
    app._resume_connect_worker = None
    app._startup_history_path = None
    observations = {"spawn": 0, "loaded": False, "job_count": 0, "killed": 0}
    proc = SimpleNamespace(pid=7331, returncode=None)
    proc.poll = lambda: proc.returncode
    proc.wait = lambda **_: proc.returncode
    def spawn(argv, **kwargs):
        observations["spawn"] += 1
        observations["loaded"] = True
        observations["argv"] = argv
        return proc
    def kill(_proc):
        observations["killed"] += 1
        proc.returncode = 0
        observations["loaded"] = False
    original_start = strata_engine.start
    monkeypatch.setattr(strata_engine, "start", lambda settings, **kwargs: original_start(settings, **{"spawn": spawn, **kwargs}))
    monkeypatch.setattr(strata_engine, "engine_process_alive", lambda: False)
    monkeypatch.setattr(strata_engine, "memory_free_bytes", lambda: (200 * 10**9, 400 * 10**9))
    monkeypatch.setattr(strata_engine, "gpu_memory_mib", lambda: None)
    monkeypatch.setattr(strata_engine, "request_unload", lambda *_, **__: True)
    monkeypatch.setattr(strata_engine, "log_path", lambda: tmp_path / "engine.log")
    monkeypatch.setattr(strata_engine.atexit, "register", lambda *_, **__: None)
    monkeypatch.setattr(ninfer_engine, "_kill", kill)
    monkeypatch.setattr(ninfer_engine.jobkill, "create", lambda: 81)
    monkeypatch.setattr(ninfer_engine.jobkill, "assign", lambda *_: True)
    monkeypatch.setattr(ninfer_engine.jobkill, "terminate", lambda *_: kill(proc) is None)
    monkeypatch.setattr(ninfer_engine.jobkill, "active_process_count", lambda *_: observations["job_count"])
    monkeypatch.setattr(ninfer_engine.jobkill, "close", lambda *_: True)
    monkeypatch.setattr(ninfer_engine, "_JOB_DRAIN_TIMEOUT", 0.01)
    monkeypatch.setattr(ninfer_engine, "_JOB_DRAIN_INTERVAL", 0.001)
    monkeypatch.setattr("litetui.llm_backend.wait_for_exit", lambda *_args, **_kw: proc.returncode is not None)
    monkeypatch.setattr("litetui.app.harness_mod.other_live_litetui", lambda *_: None)
    monkeypatch.setattr(app.backend, "_health_state", lambda *_: "loaded" if observations["loaded"] else None)
    monkeypatch.setattr(app.backend, "_health", lambda *_: observations["loaded"])
    monkeypatch.setattr(app.backend, "_get_json", lambda *_: {"data": [{"id": "old-model", "status": {"value": "loaded"}}]})
    app._engine_observations = observations
    yield app


def common(app):
    current = native_engine.state(app)
    return {"version": 1, "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
            "backend": app.backend.name, "desired_model": app.model_id, "management_instance": current.instance}


def review(app, action="start"):
    snapshot = native_engine.inspect(app, common(app))
    assert snapshot["launch"] is not None, snapshot["actions"]
    request = {**common(app), "snapshot": snapshot["snapshot"], "generation": snapshot["generation"],
               "action": action, "selection_id": snapshot["launch"]["selection_id"] if action == "start" else None,
               "handle_token": snapshot["ownership"]["handle_token"] if action == "stop" else None,
               "consent_scope": "one_" + action}
    checked = native_engine.validate(app, request)
    return {**request, "review": checked["review"], "action_id": checked["action_id"]}


@pytest.mark.asyncio
async def test_inspect_review_are_pure_and_capture_frozen_plan(engine_app, monkeypatch):
    app = engine_app
    before = disk(app)
    monkeypatch.setattr(app.backend, "engine_status", forbidden)
    monkeypatch.setattr(app.backend, "_health", forbidden)
    monkeypatch.setattr(strata_engine, "memory_free_bytes", forbidden)
    monkeypatch.setattr(strata_engine, "gpu_memory_mib", forbidden)
    command = review(app)
    assert disk(app) == before and app._engine_observations["spawn"] == 0
    saved = native_engine.state(app).reviews[command["review"]][1]
    app.settings.strata_max_context = 16384
    assert saved.settings().strata_max_context is None
    first = saved.strata_inspection()
    first.config["args"].append("--unreviewed")
    assert "--unreviewed" not in saved.strata_inspection().config["args"]


@pytest.mark.asyncio
async def test_actual_lifecycle_start_connect_stop_and_readback(engine_app):
    app = engine_app
    history, policy = copy.deepcopy(app.conversation), gui_rpc._tool_policy(app)
    old_client = app.client
    started = await gui_rpc.async_dispatch(app, {"type": "gui.engine.native_action", **review(app)})
    assert started["status"] == "started", started
    assert started["spawn_attempted"] and started["worker_settled"]
    assert native_engine.submission_refusal(app) is None
    back = await native_engine.readback(app, {**common(app), "action_id": started["action_id"], "review": started["review"], "generation": 1})
    assert back["connected"] and back["loaded_model"] == "old-model"
    assert old_client.is_closed() and app.conversation == history and gui_rpc._tool_policy(app) == policy
    stopped = await native_engine.action(app, review(app, "stop"))
    assert stopped["status"] == "stopped" and stopped["shutdown"]["tree"] == "confirmed", stopped
    assert app.backend._owned is None and native_engine.submission_refusal(app)
    assert app._launch_options.server_mode == "connect" and not app._launch_options.load_model


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["settings", "history", "policy", "file", "held"])
async def test_consumed_stale_review_refuses_before_spawn(engine_app, change):
    app = engine_app
    request = review(app)
    if change == "settings":
        app.settings.strata_max_context = 16384
    elif change == "history":
        app.conversation.append({"role": "user", "content": "changed"})
    elif change == "policy":
        app.tools_enabled = True
    elif change == "file":
        Path(app.settings.strata_config).write_text("{}", encoding="utf-8")
    else:
        app._pending_input.append({"content": "hold"})
    before = disk(app)
    result = await native_engine.action(app, request)
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"], result
    assert result["generation"] == 1 and app._engine_observations["spawn"] == 0 and disk(app) == before
    back = await native_engine.readback(app, {**common(app), "action_id": request["action_id"], "review": request["review"], "generation": 1})
    assert not back["connected"]


@pytest.mark.asyncio
async def test_uncertain_job_tree_retains_same_handle_for_explicit_stop_retry(engine_app):
    app = engine_app
    assert (await native_engine.action(app, review(app)))["status"] == "started"
    handle = app.backend._owned
    app._engine_observations["job_count"] = None
    first = await native_engine.action(app, review(app, "stop"))
    assert first["status"] == "retained" and first["admission"] == "retained_recovery", first
    assert app.backend._owned is handle and native_engine.submission_refusal(app)
    app._engine_observations["job_count"] = 0
    retry = await native_engine.action(app, review(app, "stop"))
    assert retry["status"] == "stopped" and retry["shutdown"]["main_exited"], retry
    assert app._engine_observations["spawn"] == 1


@pytest.mark.asyncio
async def test_resource_confirmation_is_visible_refusal_without_unanswered_event(engine_app, monkeypatch):
    app = engine_app
    monkeypatch.setattr("litetui.app.harness_mod.other_live_litetui", lambda *_: "controlled sibling")
    async def absent(_):
        return None
    monkeypatch.setattr(app.backend, "model_info", absent)
    events = []
    app._rpc_emit = events.append
    result = await native_engine.action(app, review(app))
    assert result["status"] == "refused" and result["error_code"] == "resource_confirmation_required", result
    assert not result["spawn_attempted"] and not result["mutated"] and not getattr(app, "_gui_model_pending", {})
    assert not any(event.get("type") == "model_load_requested" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["accept", "admit", "submit", "dispatch", "profile", "execution"])
async def test_management_barrier_precedes_hooks_append_and_authority(engine_app, monkeypatch, entry):
    app = engine_app
    before, history, authority = disk(app), copy.deepcopy(app.conversation), app._agent_session.authority
    monkeypatch.setattr(hook_host, "dispatch", forbidden)
    monkeypatch.setattr(app, "_append", forbidden)
    monkeypatch.setattr(app, "_materialise_convo", forbidden)
    item = {"content": "never admitted", "source": "rpc", "tool_profile": "strict"}
    if entry == "accept":
        assert hook_host.accept_prompt_origin(app, item) is None
    elif entry == "admit":
        assert await hook_host.admit_prompt_origin(app, item) is None
    elif entry == "submit":
        app._submit_text(item["content"], alt_chord=False, source="rpc")
    elif entry == "dispatch":
        with pytest.raises(ValueError):
            gui_rpc.dispatch(app, {"type": "gui.settings.apply", "values": {"max_tokens_chat": 12}})
    elif entry == "profile":
        with pytest.raises(ValueError):
            app._remember_for_this_convo("tool_policy_profile", "autonomous")
    else:
        with pytest.raises(ValueError):
            app._validate_owned_execution()
    assert disk(app) == before and app.conversation == history and app._agent_session.authority == authority


@pytest.mark.asyncio
async def test_plan_file_drift_refuses_and_never_materializes_derived_config(engine_app):
    app = engine_app
    app.settings.strata_max_context = 16384
    plan = engine_review_plan.inspect(app.settings, "strata", app.model_id)
    derived = Path(plan.summary["derived_config"])
    assert not derived.exists()
    Path(plan.summary["artifact"]).write_bytes(b"changed")
    with pytest.raises(BackendError, match="changed"):
        strata_engine.start(app.settings, healthy=forbidden, spawn=forbidden, reviewed_plan=plan)
    assert not derived.exists()


@pytest.mark.asyncio
async def test_cancelled_start_joins_publication_and_retains_for_new_stop(engine_app, monkeypatch):
    app = engine_app
    entered, release = threading.Event(), threading.Event()
    original = strata_engine.start
    def delayed(settings, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(settings, **kwargs)
    monkeypatch.setattr(strata_engine, "start", delayed)
    task = asyncio.create_task(native_engine.action(app, review(app)))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert app.backend._starting and native_engine.state(app).pending
    release.set()
    result = await task
    assert result["status"] == "cancelled" and result["worker_settled"], result
    assert result["admission"] == "retained_recovery" and app.backend._owned is not None
    assert (await native_engine.action(app, review(app, "stop")))["status"] == "stopped"

@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["deliver", "queued", "flush"])
async def test_held_queue_is_never_popped_while_stopped(engine_app, entry):
    app = engine_app
    item = {"content": "retain exactly", "source": "rpc"}
    app._pending_input.append(item)
    before = copy.deepcopy(app.conversation)
    if entry == "deliver":
        assert not app._deliver_queued_input()
    elif entry == "queued":
        assert not await hook_host.queued_prompt(app)
    else:
        app._flush_pending_input()
    assert app._pending_input == [item] and app._pending_input[0] is item
    assert app.conversation == before


@pytest.mark.asyncio
async def test_admission_rechecks_after_lifecycle_await_and_retains_queue(engine_app, monkeypatch):
    app = engine_app
    current = native_engine.state(app)
    current.admission, current.effective_model = "prompt_ready", app.model_id
    item = {"content": "retain after yield", "source": "rpc"}
    app._pending_input.append(item)
    monkeypatch.setattr(hook_host, "snapshot", lambda _: SimpleNamespace(hooks=[object()], error=None))
    async def stop_after_await(_):
        await asyncio.sleep(0)
        current.admission, current.effective_model = "management_only", None
    monkeypatch.setattr(hook_host, "drain_lifecycle", stop_after_await)
    monkeypatch.setattr(hook_host, "dispatch", forbidden)
    monkeypatch.setattr(app, "_append", forbidden)
    assert not await hook_host.queued_prompt(app)
    assert app._pending_input == [item] and app._pending_input[0] is item


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["health", "model", "policy", "authority"])
async def test_readback_failure_latches_source_prompt_admission(engine_app, monkeypatch, fault):
    app = engine_app
    started = await native_engine.action(app, review(app))
    assert started["status"] == "started"
    if fault == "health":
        monkeypatch.setattr(app.backend, "_health", lambda *_: False)
    else:
        original = app.backend.list_models
        async def changed():
            rows = await original()
            if fault == "model":
                return []
            if fault == "policy":
                app.tools_enabled = True
            else:
                app._agent_session.update_execution(backend="strata", model="changed-model", thinking_level="default")
            return rows
        monkeypatch.setattr(app.backend, "list_models", changed)
    with pytest.raises(ValueError):
        await native_engine.readback(app, {**common(app), "action_id": started["action_id"],
                                          "review": started["review"], "generation": 1})
    assert native_engine.submission_refusal(app) and not app._gui_connection_success
    assert app.backend._owned is not None


@pytest.mark.asyncio
async def test_installed_resource_hook_keeps_actual_model_key_and_retained_claims(engine_app):
    from contextlib import asynccontextmanager
    app = engine_app
    keys = []
    @asynccontextmanager
    async def admission(key, *, reload=False):
        keys.append((key, reload))
        yield
    app.backend.resource_admission = admission
    started = await native_engine.action(app, review(app))
    assert started["status"] == "started" and keys == [("old-model", False)]
    retained = SimpleNamespace(unsettled_loads={"old-model": "reservation"}, leases={}, active_loads=set())
    app.backend._admission_session = retained
    stopped = await native_engine.action(app, review(app, "stop"))
    assert stopped["status"] == "stopped"
    assert retained.unsettled_loads == {"old-model": "reservation"}
    snapshot = native_engine.inspect(app, common(app))
    assert not snapshot["actions"]["start"]["eligible"]
    assert "reservation" in snapshot["actions"]["start"]["reason"]


@pytest.mark.asyncio
async def test_review_rechecks_source_binding_after_resource_gate_await(engine_app):
    from contextlib import asynccontextmanager
    app = engine_app
    @asynccontextmanager
    async def admission(_key, **_kwargs):
        await asyncio.sleep(0)
        app.settings.strata_max_context = 16384
        yield
    app.backend.resource_admission = admission
    result = await native_engine.action(app, review(app))
    assert result["status"] == "refused" and not result["spawn_attempted"]
    assert app._engine_observations["spawn"] == 0


@pytest.mark.asyncio
async def test_relative_native_artifact_is_bound_to_server_cwd(engine_app, monkeypatch, tmp_path):
    app = engine_app
    config = Path(app.settings.strata_config)
    data = json.loads(config.read_text())
    data["args"][1] = "model.gguf"
    config.write_text(json.dumps(data), encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "model.gguf").write_bytes(b"wrong artifact")
    monkeypatch.chdir(other)
    plan = engine_review_plan.inspect(app.settings, "strata", app.model_id)
    assert Path(plan.summary["artifact"]) == config.parent / "model.gguf"
    assert plan.strata_inspection().config["args"][1] == str(config.parent / "model.gguf")
    assert strata_engine.native_bytes(plan.strata_inspection().config) == (config.parent / "model.gguf").stat().st_size


@pytest.mark.asyncio
async def test_executable_path_and_handle_identity_are_stable_on_windows(engine_app):
    plan = engine_review_plan.inspect(engine_app.settings, "strata", engine_app.model_id)
    assert plan.summary["executable"].endswith("python.exe")
    plan.verify()


@pytest.mark.asyncio
async def test_actual_bounded_readers_stop_before_oversized_payload(engine_app, tmp_path):
    import struct
    cfg = tmp_path / "oversized.json"
    cfg.write_bytes(b"{" + b" " * 2048)
    assert strata_engine.read_config(cfg, max_bytes=1024) is None
    artifact = tmp_path / "oversized.ninfer"
    head = bytearray(ninfer_engine.NINFER_V3_HEADER_BYTES)
    head[:8] = ninfer_engine.NINFER_V3_MAGIC
    struct.pack_into("<Q", head, 8, 2048)
    artifact.write_bytes(head + b" " * 2048)
    assert ninfer_engine.read_artifact_directory(artifact, max_bytes=1024) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["gui.engine.start", "gui.engine.stop", "gui.models.reconnect",
                                       "gui.models.select", "gui.backend.set", "gui.settings.apply",
                                       "gui.conversations.open", "gui.conversations.create"])
async def test_legacy_mutation_cannot_bypass_live_engine_capability(engine_app, operation):
    app = engine_app
    assert (await native_engine.action(app, review(app)))["status"] == "started"
    handle, generation = app.backend._owned, native_engine.state(app).generation
    before, history = disk(app), copy.deepcopy(app.conversation)
    with pytest.raises(ValueError, match="reviewed native controls"):
        await gui_rpc.async_dispatch(app, {"type": operation, "name": "custom", "model": "other"})
    assert app.backend._owned is handle and native_engine.state(app).generation == generation
    assert disk(app) == before and app.conversation == history and native_engine.submission_refusal(app) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["backend", "session", "model", "authority"])
async def test_direct_runtime_identity_drift_cannot_leave_prompt_ready(engine_app, drift):
    app = engine_app
    assert (await native_engine.action(app, review(app)))["status"] == "started"
    current = native_engine.state(app)
    if drift == "backend":
        app.backend = StrataBackend(app.settings)
    elif drift == "session":
        current.session_id = "0" * 32
    elif drift == "model":
        app._model_id = "changed"
    else:
        app._agent_session.update_execution(backend="strata", model="changed", thinking_level="default")
    assert native_engine.submission_refusal(app)
    with pytest.raises(ValueError):
        app._validate_owned_execution()
    assert current.backend._owned is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("after_spawn", [False, True])
async def test_canonical_start_cooperative_cancel_preserves_shutdown_proof(engine_app, after_spawn):
    app = engine_app
    plan = engine_review_plan.inspect(app.settings, "strata", app.model_id)
    def cancelled():
        return not after_spawn or app._engine_observations["spawn"] > 0
    with pytest.raises(BackendError, match="cancelled"):
        strata_engine.start(app.settings, healthy=app.backend._health, reviewed_plan=plan, cancelled=cancelled)
    assert app._engine_observations["spawn"] == int(after_spawn)
    if after_spawn:
        assert app._engine_observations["killed"] >= 1


@pytest.mark.asyncio
async def test_frozen_config_is_materialized_not_reread_by_child(engine_app):
    app = engine_app
    plan = engine_review_plan.inspect(app.settings, "strata", app.model_id)
    owned = strata_engine.start(app.settings, healthy=app.backend._health, reviewed_plan=plan)
    argv = app._engine_observations["argv"]
    selected = Path(argv[argv.index("--config") + 1])
    assert selected != Path(app.settings.strata_config)
    assert selected == Path(plan.summary["derived_config"])
    assert json.loads(selected.read_text()) == plan.strata_inspection().config
    strata_engine.terminate_owned(owned)

@pytest.mark.asyncio
async def test_explicit_held_cancel_unlocks_fresh_review_without_replaying(engine_app):
    from test_rust_restored_input import seed

    from litetui import codex_steering
    app = engine_app
    stale = review(app)
    seed(app)
    codex_steering.restore_queue(app)
    snapshot = native_engine.inspect(app, common(app))
    assert not snapshot["actions"]["start"]["eligible"]
    view = await gui_rpc.async_dispatch(app, {"type": "gui.restored_input.list", "session_id": app.convo_id})
    assert view["held"] == 1 and view["entries"][0]["can_cancel"]
    assert not view["entries"][0]["can_continue"]
    command = {"session_id": app.convo_id, "snapshot": view["snapshot"], "entry_id": "saved-input"}
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, {"type": "gui.restored_input.continue", **command})
    reply = await gui_rpc.async_dispatch(app, {"type": "gui.restored_input.cancel", **command})
    assert reply["cancelled"] == "saved-input" and not app._pending_input
    assert native_engine.submission_refusal(app)
    assert (await native_engine.action(app, stale))["status"] == "refused"
    assert (await native_engine.action(app, review(app)))["status"] == "started"
    assert not any(message.get("role") == "user" and message.get("content") == "UNSENT SAVED INPUT" for message in app.conversation)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,reason", [("ninfer", "engine_unavailable"), ("strata", "engine_unavailable"), ("strata", "engine_unloaded")])
async def test_actual_backend_unavailable_has_typed_management_reason(engine_app, monkeypatch, backend, reason):
    from litetui.engine_lifecycle import EngineUnavailable
    from litetui.ninfer_backend import NInferBackend
    app = engine_app
    if backend == "ninfer":
        app.settings.ninfer_host = "http://127.0.0.1:45678"
        target = NInferBackend(app.settings)
        monkeypatch.setattr(target, "_health", lambda *_: False)
    else:
        target = app.backend
        monkeypatch.setattr(target, "_health_state", lambda *_: "unloaded" if reason == "engine_unloaded" else None)
    with pytest.raises(EngineUnavailable) as error:
        await target.ensure_running()
    assert error.value.reason == reason


@pytest.mark.asyncio
async def test_startup_hydration_exemption_never_grants_prompt_or_discards_transition_failure(engine_app):
    from litetui import backend_transition
    app = engine_app
    app._native_engine_state = None
    assert backend_transition.submission_refusal(app)
    assert backend_transition.submission_refusal(app, initializing=True) is None
    assert native_engine.submission_refusal(app)
    app._backend_transition_error = "Existing fatal ownership error"
    assert backend_transition.submission_refusal(app, initializing=True) == "Existing fatal ownership error"
