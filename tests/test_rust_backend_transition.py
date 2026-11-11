"""Controlled real owned backend transitions; no providers, hardware or server starts."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import socket
import subprocess
import urllib.request

import httpx
import pytest
import pytest_asyncio

from litetui import (
    backend_catalog,
    backend_transition,
    gpu_gate,
    gui_rpc,
    hook_host,
    llm_backend,
    settings,
    store_identity,
    ttyguard,
)
from litetui.app import LiteTUI
from litetui.custom_backend import CustomBackend


def forbidden(*_args, **_kwargs):
    raise AssertionError("Unexpected provider, network, hardware, process or load boundary")


@pytest_asyncio.fixture
async def switch_app(make_app, monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "controlled-host")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(ttyguard, "run", forbidden)
    monkeypatch.setattr(ttyguard, "popen", forbidden)
    monkeypatch.setattr(gpu_gate, "nvidia_smi", lambda: None)
    gpu_gate.is_rtx_5090.cache_clear()
    monkeypatch.setattr(hook_host, "enter_conversation", lambda *_: None)
    monkeypatch.setattr(hook_host, "leave_conversation", lambda *_: None)
    settings.save(settings.Settings(
        backend="custom", default_model="old-model", thinking_level="default",
        custom_base_url="http://127.0.0.1:45123/v1", lm_host="http://127.0.0.1:45124",
        tools_enabled=False, mcp_enabled=False, skills_enabled=False,
        tool_policy_profile="strict", codex_native_engine=False,
    ))
    app = make_app(initial_backend="custom", initial_model="old-model",
                   initial_thinking="default", hold_restored_input=True)
    app._model_id = "old-model"
    app._rpc = True
    app._gui_rpc_enabled = True
    app._system = lambda *_: None
    app._update_header = lambda: None
    app._fetch_ctx_window = lambda: None
    app._refresh_ctx_label = lambda: None
    app._rpc_emit_model_state = lambda: None
    app._sync_fleet_identity = lambda: None
    app._chat_running = lambda: False
    app._cli_thinking_level = "default"
    app._cli_effective_thinking = "default"
    app._thinking_level = None
    app._cli_args_done = asyncio.Event()
    app._cli_args_done.set()
    app._gui_connection_success = True
    app._connect_settled = True
    app.conversation = [{"role": "system", "content": "Fixture system"},
                        {"role": "user", "content": "Preserve history"}]
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    app.available_models = ["old-model"]
    app.model_rows = {"old-model": llm_backend.ModelRow("old-model", None, "fixture", True)}

    async def listing(_backend):
        return [llm_backend.ModelRow(key, None, "fixture", True)
                for key in ("old-model", "new-model")]

    monkeypatch.setattr(llm_backend.LMStudioBackend, "list_models", listing)
    monkeypatch.setattr(CustomBackend, "_get", lambda _backend, _path: {
        "data": [{"id": "old-model"}, {"id": "new-model"}],
    })
    monkeypatch.setattr(llm_backend.LMStudioBackend, "load", forbidden)
    monkeypatch.setattr(llm_backend.LlamaCppBackend, "_spawn", forbidden)
    monkeypatch.setattr(app, "_probe_thinking", forbidden)

    class Connection:
        async def wait(self):
            await inspect.unwrap(LiteTUI.connect)(app)

    app.connect = Connection
    try:
        yield app
    finally:
        await app.client.close()
        gpu_gate.is_rtx_5090.cache_clear()


def command(app, **changes):
    catalog = backend_catalog.catalog(app, {"session_id": app.convo_id})
    return {"type": "gui.backend.set", "id": "switch-operation", "version": 1,
            "session_id": app.convo_id, "agent_id": catalog["agent_id"],
            "snapshot": catalog["snapshot"], "name": "lmstudio", "model": "new-model", **changes}


def disk(app):
    return {str(path): path.read_bytes() for path in app._agent_session.store.data_root.rglob("*")
            if path.is_file() and not path.name.endswith(".lease")}


@pytest.mark.asyncio
async def test_success_publishes_backend_and_model_together_and_preserves_owned_history_policy(switch_app):
    app = switch_app
    request = command(app)
    before_history = copy.deepcopy(app.conversation)
    before_policy = gui_rpc._tool_policy(app)
    old_client = app.client
    global_bytes = (app._agent_session.store.data_root / "settings.json").read_bytes()
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "connected", result
    assert result["mutated"] and not result["close_required"]
    assert result["snapshot"] == request["snapshot"]
    assert (result["backend"], result["model"]) == ("lmstudio", "new-model")
    assert (app._agent_session.authority.backend, app._agent_session.authority.model) == ("lmstudio", "new-model")
    assert app.conversation == before_history and gui_rpc._tool_policy(app) == before_policy
    assert old_client.is_closed()
    assert app._launch_options.server_mode == "connect" and not app._launch_options.load_model
    assert app._cli_initial_backend is None and app._cli_initial_model is None
    assert app._user_model_choice == "new-model"
    assert (app._agent_session.store.data_root / "settings.json").read_bytes() == global_bytes
    assert backend_catalog.catalog(app, {"session_id": app.convo_id})["snapshot"] != request["snapshot"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["version", "missing-model", "empty-model", "unknown", "hidden",
                                   "session", "agent", "snapshot", "partial"])
async def test_invalid_guarded_requests_refuse_without_disk_or_runtime_changes(switch_app, fault):
    app = switch_app
    request = command(app)
    if fault == "version":
        request["version"] = True
    elif fault == "missing-model":
        del request["model"]
    elif fault == "empty-model":
        request["model"] = " "
    elif fault == "unknown":
        request["name"] = "unknown"
    elif fault == "hidden":
        request["name"] = "ninfer"
    elif fault in {"session", "agent"}:
        request[fault + "_id"] = "11111111-2222-1333-8444-555555555555"
    elif fault == "snapshot":
        request["snapshot"] = "0" * 64
    else:
        request = {"type": "gui.backend.set", "name": "lmstudio", "model": "new-model"}
    before = disk(app)
    backend = app.backend
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "refused", result
    assert not result["mutated"] and not result["close_required"]
    assert app.backend is backend and app.model_id == "old-model" and disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hold", "pending", "uncertain", "owned-server", "custom-server",
                                   "approval", "work"])
async def test_work_and_lifecycle_guards_refuse_before_candidate_io(switch_app, monkeypatch, fault):
    app = switch_app
    request = command(app)
    if fault == "hold":
        app._hold_restored_input = False
    elif fault == "pending":
        app._pending_input.append({"content": "Keep held", "_restored_input_held": True})
    elif fault == "uncertain":
        app._restored_input_error = True
    elif fault == "owned-server":
        app.backend._owned = object()
    elif fault == "custom-server":
        app._custom_server_process = object()
    elif fault == "approval":
        app._approval_waiters = {"pending": asyncio.get_running_loop().create_future()}
    else:
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    monkeypatch.setattr(llm_backend, "make_backend", forbidden)
    before = disk(app)
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "refused" and not result["close_required"], result
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["global", "conversation", "effective", "launch", "policy", "history"])
async def test_snapshot_binds_disk_policy_launch_and_history(switch_app, change):
    app = switch_app
    request = command(app)
    if change in {"global", "conversation"}:
        path = (app._agent_session.store.data_root if change == "global" else app.convo_dir) / "settings.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["snapshot_test"] = "changed"
        path.write_text(json.dumps(raw), encoding="utf-8")
    elif change == "effective":
        app.settings.max_tokens_chat += 1
    elif change == "launch":
        from litetui.launch_options import LaunchOptions
        app._launch_options = LaunchOptions(server_mode="start")
    elif change == "policy":
        app.tools_enabled = not app.tools_enabled
    else:
        app.conversation[-1]["content"] = "Changed"
    before = disk(app)
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "refused" and not result["close_required"], result
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing-model", "cold-model", "connection", "thinking"])
async def test_candidate_failure_is_fatal_without_claiming_authority_publication(switch_app, monkeypatch, failure):
    app = switch_app
    if failure == "thinking":
        app.thinking_level = "low"
    request = command(app)

    async def listing(_backend):
        if failure == "connection":
            raise llm_backend.BackendError("Controlled connection failure")
        return [llm_backend.ModelRow("different" if failure == "missing-model" else "new-model",
                                    None, "fixture", failure != "cold-model")]

    monkeypatch.setattr(llm_backend.LMStudioBackend, "list_models", listing)
    before = (app._agent_session.authority.backend, app._agent_session.authority.model)
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "failed" and result["close_required"], result
    assert not result["mutated"]
    assert (app._agent_session.authority.backend, app._agent_session.authority.model) == before
    assert app._backend_transition_error


@pytest.mark.asyncio
async def test_partial_publication_is_fatal_and_does_not_claim_rollback(switch_app, monkeypatch):
    from litetui import settings_service

    app = switch_app
    request = command(app)
    write = settings_service._write
    def fail_snapshot(path, raw):
        if path == app.convo_dir / "settings.json":
            raise OSError("Controlled historical snapshot failure")
        return write(path, raw)
    monkeypatch.setattr(settings_service, "_write", fail_snapshot)
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], result
    assert "historical snapshot failure" in result["error"]
    assert app.backend.name == "custom"
    assert (app._agent_session.authority.backend, app._agent_session.authority.model) == ("lmstudio", "new-model")


@pytest.mark.asyncio
async def test_fatal_latch_blocks_gui_and_non_gui_escape_routes(switch_app, monkeypatch):
    app = switch_app
    app._backend_transition_error = backend_transition.CLOSE_REQUIRED
    refused = []
    monkeypatch.setattr(app, "_refuse_submit", lambda kind, reason: refused.append((kind, reason)))
    before = copy.deepcopy(app.conversation)
    original_backend = app.backend
    LiteTUI._submit_text(app, "must not send", False)
    LiteTUI._handle_command(app, "/new")
    LiteTUI.apply_backend_change(app, "lmstudio")
    assert LiteTUI._resume(app, app.convo_path) is False
    app._pending_input.append({"content": "Retained"})
    LiteTUI._flush_pending_input(app)
    with pytest.raises(ValueError, match="close this runtime"):
        app._validate_owned_execution()
    with pytest.raises(ValueError, match="close this runtime"):
        await gui_rpc.async_dispatch(app, {"type": "gui.models.reconnect"})
    assert refused and app.backend is original_backend and app.conversation == before
    assert app._pending_input == [{"content": "Retained"}]


@pytest.mark.asyncio
async def test_cancelled_candidate_connection_latches_closed_without_publication(switch_app, monkeypatch):
    app = switch_app
    request = command(app)

    async def cancelled(_backend):
        raise asyncio.CancelledError()

    monkeypatch.setattr(llm_backend.LMStudioBackend, "list_models", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await gui_rpc.async_dispatch(app, request)
    assert app._backend_transition_error
    assert (app._agent_session.authority.backend, app._agent_session.authority.model) == ("custom", "old-model")
    assert app._backend_transition_inflight is False


@pytest.mark.asyncio
async def test_exception_after_home_publication_stays_fatal_and_potentially_mutated(switch_app, monkeypatch):
    from litetui.settings_service import SettingsService

    app = switch_app
    request = command(app)
    original = SettingsService.save_patch

    def save_then_fail(service, *args, **kwargs):
        original(service, *args, **kwargs)
        raise OSError("Controlled error after publication")

    monkeypatch.setattr(SettingsService, "save_patch", save_then_fail)
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], result
    assert "error after publication" in result["error"]
    assert (app._agent_session.authority.backend, app._agent_session.authority.model) == ("lmstudio", "new-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,location", [("codex", "memory"), ("codex", "disk"), ("claude", "disk")])
async def test_hidden_pending_journal_blocks_switch_without_restoring_or_sending(switch_app, provider, location):
    app = switch_app
    if provider == "codex":
        entry = {"id": "hidden-delivery", "state": "uncertain", "revision": 1,
                 "item": {"content": "Retained journal input"}}
        message = {"role": "assistant", "content": "Previous answer",
                   "provider_metadata": {"steering": [entry]}}
        if location == "memory":
            app.conversation.append(message)
        else:
            app.store.write_record({"type": "msg", "message": message})
    else:
        from litetui.claude_persistence import ClaudeLedger
        ledger = ClaudeLedger(app.convo_dir)
        segment = ledger.select_segment(str(app._agent_session.memory_root))
        ledger.prepare(segment["id"], "Retained Claude input", "strict", "rpc", "hidden-operation")
    request = command(app)
    before = disk(app)
    result = await gui_rpc.async_dispatch(app, request)
    assert result["status"] == "refused" and not result["close_required"], result
    assert "saved" in result["error"]
    assert disk(app) == before and app._pending_input == []


@pytest.mark.asyncio
async def test_catalog_does_not_publish_a_model_that_disagrees_with_owned_authority(switch_app):
    app = switch_app
    app._model_id = "unpublished-model"
    with pytest.raises(ValueError, match="model disagrees"):
        backend_catalog.catalog(app, {"session_id": app.convo_id})


@pytest.mark.asyncio
@pytest.mark.parametrize("reader_fails", [False, True])
async def test_codex_cleanup_awaits_reader_after_process_exit(reader_fails):
    from types import SimpleNamespace

    from litetui.codex_app_server import AppServer

    server = AppServer()
    server.process = SimpleNamespace(returncode=0)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def reader():
        entered.set()
        await release.wait()
        if reader_fails:
            raise RuntimeError("Controlled reader cleanup failure")

    server.reader = asyncio.create_task(reader())
    backend = SimpleNamespace(name="codex", app_server=server, shutdown=server.shutdown)
    cleanup = asyncio.create_task(backend_transition._close_backend(SimpleNamespace(), backend))
    try:
        await entered.wait()
        await asyncio.sleep(0)
        assert not cleanup.done(), "Exited process must not hide an unsettled reader"
        release.set()
        if reader_fails:
            with pytest.raises(RuntimeError, match="reader cleanup failure"):
                await cleanup
        else:
            await cleanup
        assert server.reader.done()
    finally:
        release.set()
        await asyncio.gather(cleanup, server.reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_model_unloaded_during_actual_connect_cannot_report_connected(switch_app, monkeypatch):
    app = switch_app
    request = command(app)
    listings = 0

    async def listing(_backend):
        nonlocal listings
        listings += 1
        return [llm_backend.ModelRow("new-model", None, "fixture", listings == 1)]

    monkeypatch.setattr(llm_backend.LMStudioBackend, "list_models", listing)
    result = await gui_rpc.async_dispatch(app, request)
    assert listings == 2
    assert result["status"] == "failed" and result["mutated"] and result["close_required"], result
    assert "not loaded after connection" in result["error"]
    assert app._backend_transition_error


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [None, True, False, 0, -1, 1048577, 12.5, "1024"])
async def test_conversation_read_rejects_invalid_budget_before_read(switch_app, monkeypatch, budget):
    monkeypatch.setattr(gui_rpc, "_read_session", forbidden)
    with pytest.raises(ValueError, match="max_response_bytes must be"):
        await gui_rpc.async_dispatch(switch_app, {
            "type": "gui.conversations.read", "session_id": switch_app.convo_id,
            "max_response_bytes": budget,
        })


@pytest.mark.asyncio
async def test_bounded_history_read_uses_full_ascii_wire_size_and_preserves_session(switch_app, monkeypatch):
    import io

    from litetui import rpc

    app = switch_app
    app.store.write_record({"type": "msg", "message": {"role": "user", "content": "\U0001f9e0" * 10000}})
    request = {"type": "gui.conversations.read", "id": "bounded-\U0001f9e0", "session_id": app.convo_id}
    original_backend, original_store = app.backend, app.store
    before = disk(app)
    result = await gui_rpc.async_dispatch(app, request)
    output = io.StringIO()
    monkeypatch.setattr(rpc, "_real_stdout", output)
    rpc._respond(request["id"], ok=True, result=result)
    wire_size = len(output.getvalue().rstrip("\n").encode("utf-8"))
    assert wire_size > len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    assert wire_size < 1048576
    assert await gui_rpc.async_dispatch(app, {**request, "max_response_bytes": wire_size}) == result
    with pytest.raises(ValueError, match="exceeds max_response_bytes"):
        await gui_rpc.async_dispatch(app, {**request, "max_response_bytes": wire_size - 1})
    assert app.backend is original_backend and app.store is original_store
    assert disk(app) == before and not getattr(app, "_backend_transition_error", None)
    assert await gui_rpc.async_dispatch(app, request) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["absent", "legacy", "v1"])
async def test_backend_switch_preserves_negotiated_history_protocol(switch_app, mode):
    from litetui import native_history
    from litetui.launch_options import LaunchOptions

    app = switch_app
    version = 1 if mode == "v1" else None
    app._launch_options = None if mode == "absent" else LaunchOptions(history_projection_version=version)
    messages = copy.deepcopy(app.conversation)

    def history_command(operation):
        return {"type": operation, "version": 1, "from_session_id": app.convo_id,
                "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
                "backend": app.backend.name, "model": app.model_id,
                "scope": "current", "catalog_snapshot": None}

    before = gui_rpc.dispatch(app, history_command("gui.history.native_snapshot")) if version else None
    result = await gui_rpc.async_dispatch(app, command(app))
    assert result["status"] == "connected", result
    if version:
        assert before is not None
        after = gui_rpc.dispatch(app, history_command("gui.history.native_snapshot"))
        assert after["context_sha256"] == before["context_sha256"]
        assert after["replay_sha256"] == before["replay_sha256"]
        assert after["snapshot"] != before["snapshot"]
        page = gui_rpc.dispatch(app, {**history_command("gui.history.native_page"),
                                     "snapshot": after["snapshot"], "anchor": "tail"})
        assert page["at_end"] and page["fragments"][-1]["text"] == "Preserve history"
    else:
        with pytest.raises(ValueError, match="Restart with history projection"):
            gui_rpc.dispatch(app, history_command("gui.history.native_snapshot"))
        legacy = gui_rpc.dispatch(app, {"type": "gui.conversations.read", "session_id": app.convo_id})
        assert legacy["messages"] == messages
    assert app._launch_options.history_projection_version == version
    assert native_history.enabled(app) is (version == 1)
    assert app.conversation == messages
    assert app._launch_options.server_mode == "connect" and app._launch_options.load_model is False
    for path in (app.convo_dir / "settings.json", app._agent_session.store.data_root / "settings.json"):
        assert "history_projection_version" not in json.loads(path.read_text(encoding="utf-8"))
