"""Source-owned backend metadata with GPU/process/provider tripwires."""
from __future__ import annotations

import json
import socket
import subprocess
import urllib.request
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from litetui import (
    backend_catalog,
    gpu_gate,
    gui_rpc,
    hook_host,
    llm_backend,
    settings,
    store_identity,
    ttyguard,
)


def forbidden(*_args, **_kwargs):
    raise AssertionError("Backend catalog must not probe hardware, engines, models or providers")


@pytest_asyncio.fixture
async def catalog_app(make_app, monkeypatch, request):
    # Real owned construction and store, with unrelated effects explicitly barred.
    monkeypatch.setattr("platform.node", lambda: "controlled-host")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(ttyguard, "run", forbidden)
    monkeypatch.setattr(gpu_gate, "nvidia_smi", lambda: None)
    gpu_gate.is_rtx_5090.cache_clear()
    monkeypatch.setattr(hook_host, "enter_conversation", lambda *_: None)
    monkeypatch.setattr(hook_host, "leave_conversation", lambda *_: None)
    backend, native = getattr(request, "param", ("codex", True))
    settings.save(settings.Settings(backend=backend, default_model="fixture-model",
                                    tools_enabled=False, mcp_enabled=False, skills_enabled=False,
                                    codex_native_engine=native))
    app = make_app(initial_backend=backend, initial_model="fixture-model")
    app._model_id = "fixture-model"  # The source catalog describes a connected selection.
    app._system = lambda *_: None
    app._refresh_ctx_label = lambda: None
    app._sync_fleet_identity = lambda: None
    app._chat_running = lambda: False
    app.conversation = [{"role": "system", "content": "Fixture system"},
                        {"role": "user", "content": "Catalog fixture"}]
    app._prompt_store_identity = store_identity.capture(app)
    app._store_injected = True
    app._materialise_convo()
    # Factory/model/lifecycle access during metadata is a defect, not a fixture.
    monkeypatch.setattr(llm_backend, "make_backend", forbidden)
    monkeypatch.setattr(llm_backend, "_make_backend", forbidden)
    for method in ("list_models", "ensure_running", "load", "unload"):
        if hasattr(app.backend, method):
            monkeypatch.setattr(app.backend, method, forbidden)
    try:
        yield app
    finally:
        await app.client.close()
        gpu_gate.is_rtx_5090.cache_clear()


def read(app, **extra):
    return backend_catalog.catalog(app, {"session_id": app.convo_id, **extra})


def tree(app):
    return {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            if path.name.endswith(".lease") else path.read_bytes()
            for path in app._agent_session.store.data_root.rglob("*") if path.is_file()}


@pytest.mark.asyncio
@pytest.mark.parametrize("gpu", [None, "NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090"])
async def test_catalog_uses_real_source_visibility_labels_order_and_engine_controls(catalog_app, monkeypatch, gpu):
    app = catalog_app
    calls = []
    if gpu is not None:
        monkeypatch.setattr(gpu_gate, "nvidia_smi", lambda: "controlled-nvidia-smi")
        def output(argv, **kwargs):
            assert argv == ["controlled-nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]
            assert kwargs == {"timeout": 10}
            calls.append(argv)
            return SimpleNamespace(stdout=gpu + "\n")
        monkeypatch.setattr(ttyguard, "run", output)
    gpu_gate.is_rtx_5090.cache_clear()
    before = tree(app)
    result = read(app)
    visible = llm_backend.visible_backends()
    # Display-only readiness marks (plugins/model_switch.backend_rows) ride outside the snapshot.
    marks = result.pop("marks")
    assert set(marks) <= {row["name"] for row in result["entries"]}
    assert all(isinstance(mark, str) and mark for mark in marks.values())
    assert result == {
        "version": 1, "kind": "support_metadata", "readiness_checked": False,
        "session_id": app.convo_id, "agent_id": app._agent_session.authority.agent_id,
        "agent_name": app._agent_session.authority.name,
        "selected_backend": "codex", "configured_backend": "codex",
        "selected_model": "fixture-model", "snapshot": result["snapshot"],
        "codex_engine": {"requested_native": True, "effective": "native"},
        "entries": [{"name": name, "label": label, "selected": name == "codex",
                     "owned_engine_controls": name in llm_backend.ENGINE_BACKENDS}
                    for name, label in visible],
    }
    assert [row["name"] for row in result["entries"]] == [
        name for name, _ in llm_backend.BACKENDS if gpu and "5090" in gpu or name != "ninfer"]
    assert sum(row["selected"] for row in result["entries"]) == 1
    assert len(calls) == (0 if gpu is None else 1)  # Actual source cache, controlled probe.
    assert tree(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_app", [("codex", True), ("codex", False)], indirect=True)
async def test_effective_codex_engine_is_actual_capability_not_current_requested_setting(catalog_app):
    app = catalog_app
    native = hasattr(app.backend, "app_server")
    app.settings.codex_native_engine = not native
    result = read(app)
    assert result["codex_engine"] == {"requested_native": not native,
                                       "effective": "native" if native else "http"}
    assert result["readiness_checked"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog_app", [("custom", True), ("custom", False)], indirect=True)
async def test_inactive_codex_engine_is_unknown_even_with_configured_preference(catalog_app):
    app = catalog_app
    result = read(app)
    assert result["selected_backend"] == "custom"
    assert result["codex_engine"] == {"requested_native": app.settings.codex_native_engine,
                                       "effective": None}


@pytest.mark.asyncio
async def test_configured_backend_is_separate_from_selected_runtime(catalog_app):
    app = catalog_app
    app.settings.backend = "claude"
    result = read(app)
    assert result["configured_backend"] == "claude" and result["selected_backend"] == "codex"
    assert next(row for row in result["entries"] if row["selected"])["name"] == "codex"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["session", "no-owner", "store-owner", "loading", "persistence",
                                   "unowned", "path", "busy", "authority", "missing-backend"])
async def test_catalog_requires_current_owned_idle_runtime(catalog_app, monkeypatch, fault):
    app = catalog_app
    owner = app._agent_session
    extra = {}
    if fault == "session":
        extra["session_id"] = "11111111-2222-1333-8444-555555555555"
    elif fault == "no-owner":
        monkeypatch.setattr(app, "_agent_session", None)
    elif fault == "store-owner":
        monkeypatch.setattr(app.store, "agent_session", None)
    elif fault == "loading":
        app.store.loading = True
    elif fault == "persistence":
        app.store.persist_error = "controlled failure"
    elif fault == "unowned":
        app.store.release()
    elif fault == "path":
        app.store.convo_path = app.convo_path.parent / "wrong.jsonl"
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "authority":
        monkeypatch.setattr(app.backend, "name", "claude")
    else:
        monkeypatch.setattr(app, "_backend", None)
    try:
        with pytest.raises((ValueError, OSError)):
            read(app, **extra)
    finally:
        app._agent_session = owner


@pytest.mark.asyncio
async def test_agent_lease_loss_invalidates_catalog(catalog_app):
    app = catalog_app
    app._agent_session.release()
    with pytest.raises((ValueError, OSError), match="owned"):
        read(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, 0, 1, "false", [], {}])
async def test_requested_engine_setting_must_be_boolean(catalog_app, flag):
    catalog_app.settings.codex_native_engine = flag
    with pytest.raises(ValueError, match="boolean"):
        read(catalog_app)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["hidden-active", "hidden-configured", "unknown", "duplicate", "label",
                                   "registry-duplicate", "registry-engine", "name-budget", "label-budget",
                                   "row-budget", "response-budget"])
async def test_catalog_rejects_hidden_or_corrupt_registry_and_explicit_budgets(catalog_app, monkeypatch, fault):
    app = catalog_app
    if fault == "hidden-active":
        monkeypatch.setattr(llm_backend, "visible_backends", lambda: tuple(
            row for row in llm_backend.BACKENDS if row[0] != app.backend.name))
    elif fault == "hidden-configured":
        app.settings.backend = "ninfer"
    elif fault in ("unknown", "duplicate", "label"):
        rows = list(llm_backend.visible_backends())
        rows.append(("unknown", "Unknown") if fault == "unknown" else rows[0]
                    if fault == "duplicate" else ("codex", "Drifted label"))
        monkeypatch.setattr(llm_backend, "visible_backends", lambda: tuple(rows))
    elif fault == "registry-duplicate":
        monkeypatch.setattr(llm_backend, "BACKENDS", llm_backend.BACKENDS + llm_backend.BACKENDS[:1])
    elif fault == "registry-engine":
        monkeypatch.setattr(llm_backend, "ENGINE_BACKENDS", frozenset({"unknown"}))
    else:
        constant = {"name-budget": "MAX_NAME", "label-budget": "MAX_LABEL",
                    "row-budget": "MAX_BACKENDS", "response-budget": "MAX_RESPONSE"}[fault]
        monkeypatch.setattr(backend_catalog, constant, 1)
    with pytest.raises(ValueError):
        read(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["backend-object", "settings-object", "engine-request", "engine-capability",
                                    "configured-backend", "session-id", "lease"])
async def test_catalog_revalidates_owned_identity_and_engine_facts_after_visibility(catalog_app, monkeypatch, change):
    app = catalog_app
    visible = llm_backend.visible_backends()
    def changed():
        if change == "backend-object":
            from copy import copy
            app._backend = copy(app.backend)
        elif change == "settings-object":
            from copy import copy
            app.settings = copy(app.settings)
        elif change == "engine-request":
            app.settings.codex_native_engine = False
        elif change == "engine-capability":
            del app.backend.app_server
        elif change == "configured-backend":
            app.settings.backend = "claude"
        elif change == "session-id":
            app.convo_id = "11111111-2222-1333-8444-555555555555"
        else:
            app._agent_session.release()
        return visible
    monkeypatch.setattr(llm_backend, "visible_backends", changed)
    with pytest.raises((ValueError, OSError)):
        read(app)


@pytest.mark.asyncio
async def test_catalog_payload_fits_declared_byte_bound(catalog_app):
    payload = json.dumps(read(catalog_app), allow_nan=False).encode("utf-8")
    assert len(payload) <= backend_catalog.MAX_RESPONSE


@pytest.mark.asyncio
async def test_gui_dispatch_advertises_and_returns_catalog_without_switching(catalog_app):
    app = catalog_app
    hello = gui_rpc.dispatch(app, {"type": "gui.hello", "protocol_version": gui_rpc.PROTOCOL_VERSION})
    assert "backends.list" in hello["capabilities"]
    assert "gui.backends.list" in hello["operations"]
    before = tree(app)
    selected = app.backend
    result = gui_rpc.dispatch(app, {"type": "gui.backends.list", "session_id": app.convo_id})
    assert result == read(app)
    assert app.backend is selected and tree(app) == before
    with pytest.raises(ValueError):
        gui_rpc.dispatch(app, {"type": "gui.backends.list"})


@pytest.mark.asyncio
async def test_catalog_accepts_owned_staged_conversation_without_materializing(catalog_app):
    app = catalog_app
    app.store.stage("11111111-2222-1333-8444-555555555555")
    before = tree(app)
    result = read(app)
    assert result["session_id"] == app.convo_id
    assert app.store.pending and not app.convo_dir.exists() and tree(app) == before
