"""Connect-only contracts through the real llama backend; no network or child processes."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import launch_options, llm_backend

HOST = "http://127.0.0.1:45123"
OTHER = "http://127.0.0.1:45124"


def backend_fixture(monkeypatch, *, attach_hosts=()):
    settings = SimpleNamespace(llama_host=HOST, llama_attach_hosts=list(attach_hosts), llama_load_settings={})
    backend = llm_backend.LlamaCppBackend(settings)

    def forbidden(*args, **kwargs):
        raise AssertionError("network, process, or implicit spawn boundary was reached")

    monkeypatch.setattr(llm_backend, "llama_executable", lambda settings: Path("synthetic-server"))
    monkeypatch.setattr(llm_backend, "_http_json", forbidden)
    monkeypatch.setattr(llm_backend.ttyguard, "popen", forbidden)
    monkeypatch.setattr(llm_backend.router_record, "read", lambda: None)
    monkeypatch.setattr(backend, "_probe_shape", lambda: llm_backend._SHAPE_ROUTER)
    monkeypatch.setattr(backend, "_spawn", forbidden)
    return backend


def app_fixture(backend, *, mode="connect", explicit=False, prepared=False):
    options = launch_options.LaunchOptions(
        server_mode=mode, base_url=HOST if explicit else None
    )
    return SimpleNamespace(
        backend=backend,
        _launch_options=options,
        _launch_backend="llamacpp",
        _launch_prepared=prepared,
    )


def health_sequence(monkeypatch, observations):
    values = iter(observations)
    hosts = []

    def healthy(host):
        hosts.append(host)
        assert host == HOST, "connect-only must not probe a different server"
        return next(values)

    monkeypatch.setattr(llm_backend, "_healthy", healthy)
    return hosts


@pytest.mark.parametrize("mode,explicit", [("connect", False), ("connect", True), ("auto", True)])
def test_endpoint_dies_between_preflight_and_real_ensure(monkeypatch, mode, explicit):
    backend = backend_fixture(monkeypatch)
    hosts = health_sequence(monkeypatch, [True, False])
    app = app_fixture(backend, mode=mode, explicit=explicit)
    with pytest.raises(llm_backend.BackendError, match="no fallback or server start"):
        asyncio.run(launch_options.prepare(app))
    assert hosts == [HOST, HOST]
    assert app._launch_prepared is False


@pytest.mark.parametrize("mode,explicit", [("connect", False), ("auto", True)])
def test_already_prepared_launch_restricts_real_ensure(monkeypatch, mode, explicit):
    backend = backend_fixture(monkeypatch, attach_hosts=[OTHER])
    hosts = health_sequence(monkeypatch, [False])
    app = app_fixture(backend, mode=mode, explicit=explicit, prepared=True)
    with pytest.raises(llm_backend.BackendError, match="no fallback or server start"):
        asyncio.run(launch_options.prepare(app))
    assert hosts == [HOST]


@pytest.mark.parametrize("entry", ["prepare", "async", "sync", "preset", "owner_exit"])
def test_successful_connect_restriction_survives_reuse(monkeypatch, entry):
    backend = backend_fixture(monkeypatch, attach_hosts=[OTHER])
    hosts = health_sequence(monkeypatch, [True, True, False])
    app = app_fixture(backend)
    assert asyncio.run(launch_options.prepare(app)) == "ok"
    assert app._launch_prepared is True

    with pytest.raises(llm_backend.BackendError, match="no fallback or server start"):
        if entry == "prepare":
            asyncio.run(launch_options.prepare(app))
        elif entry == "async":
            asyncio.run(backend.ensure_running())
        elif entry == "sync":
            backend._ensure_running_sync()
        elif entry == "preset":
            backend._ensure_running_sync(preset=Path("synthetic-preset.ini"))
        else:
            monkeypatch.setattr(backend, "owner_exited", lambda: True)
            backend.recover_owner_exit()
    assert hosts == [HOST, HOST, HOST]


@pytest.mark.parametrize("mode,explicit", [("auto", False), ("start", False), ("start", True)])
def test_unrestricted_launch_keeps_existing_explicit_start_or_auto_behavior(monkeypatch, mode, explicit):
    backend = backend_fixture(monkeypatch)
    hosts = health_sequence(monkeypatch, [False])
    spawns = []
    def controlled_spawn():
        spawns.append(HOST)
        return "controlled start"

    monkeypatch.setattr(backend, "_spawn", controlled_spawn)
    app = app_fixture(backend, mode=mode, explicit=explicit)
    assert asyncio.run(launch_options.prepare(app)) == "controlled start"
    assert spawns == [HOST] and hosts == [HOST]


def test_connect_restriction_cannot_be_cleared_by_repeated_prepare(monkeypatch):
    backend = backend_fixture(monkeypatch)
    health_sequence(monkeypatch, [True, True, False])
    app = app_fixture(backend)
    assert asyncio.run(launch_options.prepare(app)) == "ok"
    app._launch_options = None
    with pytest.raises(llm_backend.BackendError, match="no fallback or server start"):
        asyncio.run(launch_options.prepare(app))


@pytest.mark.parametrize("entry", ["spawn", "preset", "load_missing", "load_settings", "load_context"])
def test_connect_only_blocks_restart_before_any_mutation(monkeypatch, entry):
    backend = backend_fixture(monkeypatch)
    backend.require_existing_server()
    effects = []

    def forbidden(*args, **kwargs):
        effects.append("mutation")
        raise AssertionError("connect-only reached preset, settings, or shutdown mutation")

    monkeypatch.setattr(llm_backend, "scan_models", forbidden)
    monkeypatch.setattr(llm_backend, "render_preset_ini", forbidden)
    monkeypatch.setattr(llm_backend, "_write_preset", forbidden)
    monkeypatch.setattr(backend, "shutdown", forbidden)
    monkeypatch.setattr(backend, "_record_load_settings", forbidden)
    monkeypatch.setattr(backend, "_server_models", dict)

    with pytest.raises(llm_backend.BackendError, match="no fallback or server start"):
        if entry == "spawn":
            llm_backend.LlamaCppBackend._spawn(backend)
        elif entry == "preset":
            backend._regen_ini(prepared="synthetic preset")
        elif entry == "load_missing":
            backend._load_sync("new-model")
        elif entry == "load_context":
            asyncio.run(backend.load("existing-model", ctx=8192))
        else:
            backend._apply_sync("existing-model", {"context_length": 4096})
    assert effects == []
    assert backend._settings.llama_load_settings == {}


def test_connect_only_allows_loading_model_already_in_existing_preset(monkeypatch):
    backend = backend_fixture(monkeypatch)
    backend.require_existing_server()
    requests = []
    monkeypatch.setattr(backend, "_server_models", lambda: {
        "existing-model": {"status": {"value": "loaded"}},
    })
    monkeypatch.setattr(llm_backend, "scan_models", lambda settings: [])
    monkeypatch.setattr(llm_backend, "resolve_speculation", lambda *args: None)

    def respond(url, body, *, timeout):
        requests.append((url, body, timeout))
        return {"success": True}

    monkeypatch.setattr(llm_backend, "_http_json", respond)
    asyncio.run(backend.load("existing-model", ctx=None))
    assert requests == [(HOST + "/models/load", {"model": "existing-model"}, 30)]


@pytest.mark.parametrize("mode,explicit,allowed", [
    ("connect", False, False), ("connect", True, False), ("auto", True, False),
    ("auto", False, True), ("start", False, True), ("start", True, True),
])
def test_source_context_validation_preserves_unrestricted_auto_and_requires_start_for_connect(mode, explicit, allowed):
    options = launch_options.LaunchOptions(
        server_mode=mode, base_url=HOST if explicit else None, load_model=True, context_length=8192
    )
    settings = SimpleNamespace(llama_host=HOST)
    if allowed:
        assert options.overrides(settings, "llamacpp", "selected")["default_context_length"] == 8192
    else:
        with pytest.raises(ValueError, match="context changes require --start-server"):
            options.overrides(settings, "llamacpp", "selected")
