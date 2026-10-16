"""Owned construction/storage gate with explicit Python-level effect tripwires.

Inherited suite fixtures redirect home, data paths and cached configuration;
product imports can precede those fixtures. This is NOT clean-import containment,
a mounted lifecycle, a launched child, provider readiness or an OS sandbox.
Ownership, constructor, backend, client and repository operations remain real.
Extension activation and external transports are substituted, not validated.
"""
from __future__ import annotations

import asyncio
import platform
import socket
import subprocess
import threading
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
import pytest
import pytest_asyncio

from litetui import (
    agent_launch_context,
    agent_ownership,
    approval_relay,
    harness,
    hook_host,
    llm_backend,
    mcp_client,
    paths,
    plugins,
    settings,
    shared_state,
    update_check,
)
from litetui.app import LiteTUI
from litetui.conversation import ConversationRepository

# Do not inherit the suite's silent MCP no-ops: this gate records accidental
# activation instead. MCP stays disabled through actual constructor settings.
pytestmark = pytest.mark.real_mcp_load
AID = "11111111-1111-4111-8111-111111111111"
NAME = "RustFixture"


class ConstructionGate:
    """Observe partial construction and keep tripwires through all cleanup."""

    def __init__(self, root, monkeypatch):
        self.root = root
        self.monkeypatch = monkeypatch
        self.app = None
        self.session = None
        self.calls = []
        self.denied = []
        self.cleaned = []

    def forbid(self, label):
        def denied(*_args, **_kwargs):
            self.denied.append(label)
            raise AssertionError(f"uncontrolled external effect: {label}")
        return denied

    def forbid_async(self, label):
        async def denied(*_args, **_kwargs):
            self.forbid(label)()
        return denied

    def cleanup(self, label, callback):
        self.cleaned.append(label)
        callback()

    async def cleanup_async(self, label, callback):
        self.cleaned.append(label)
        await callback()

    @asynccontextmanager
    async def open(self):
        root = self.root
        global_before = agent_before = home = None
        with self.monkeypatch.context() as patch:
            # Cold platform.node() enters win32_ver -> subprocess 'ver'. Only
            # substitute host metadata, never identity or ownership behavior.
            patch.setattr(platform, "node", lambda: "synthetic-host")
            for obj, name in (
                (subprocess, "Popen"), (socket, "create_connection"),
                (socket, "getaddrinfo"), (socket.socket, "connect"),
                (socket.socket, "connect_ex"), (httpx.Client, "send"),
                (update_check, "start_background_check"), (harness.Seat, "register"),
                (mcp_client.MCPManager, "load"),
                (mcp_client.MCPManager, "reload_configs"),
                (mcp_client.MCPManager, "connect"),
            ):
                patch.setattr(obj, name, self.forbid(f"{obj.__name__}.{name}"))
            patch.setattr(httpx.AsyncClient, "send", self.forbid_async("AsyncClient.send"))
            for method in ("list_models", "loaded_models", "model_info", "ensure_running"):
                patch.setattr(llm_backend.LMStudioBackend, method, self.forbid_async(method))
            patch.setattr(hook_host, "initialize", lambda app: self.calls.append("hooks-substituted"))
            patch.setattr(plugins, "register_plugins", lambda *_args, **_kwargs: [])
            for key in (harness.SPAWN_IDENTITY_MARKER, harness.OWNER_MARKER,
                        approval_relay.SPAWNED_BY_ENV, approval_relay.APPROVAL_HOST_ENV):
                patch.delenv(key, raising=False)
            patch.setenv("LITETUI_BACKEND", "codex")
            patch.setenv("LITETUI_MODEL", "poison-env")
            patch.setenv("LITETUI_THINKING", "low")
            original_init = LiteTUI.__init__

            def observed_init(app, *args, **kwargs):
                self.app = app  # Capture BEFORE any constructor code can fail.
                original_init(app, *args, **kwargs)

            patch.setattr(LiteTUI, "__init__", observed_init)
            try:
                async with AsyncExitStack() as stack:
                    defaults = settings.Settings(
                        backend="codex", default_model="poison-settings", thinking_level="low",
                        skills_enabled=False, mcp_enabled=False,
                    )
                    assert paths.data_root() == root
                    settings.save(defaults, root=root)
                    global_before = settings.settings_path().read_bytes()
                    self.session = agent_launch_context.create(
                        root, NAME, agent_id=AID, backend="lmstudio",
                        model="fixture-model", thinking_level="high",
                    )
                    stack.callback(self.cleanup, "session", self.session.release)
                    home = self.session.memory_root
                    agent_before = (home / "settings.json").read_bytes()
                    (home / "memory.md").write_text("synthetic persistent memory", encoding="utf-8")
                    try:
                        app = LiteTUI(agent_session=self.session, rpc=True)
                        yield app, self.session, self.calls
                    finally:
                        # Independent callbacks: one failure must not strand
                        # another resource. Avoid properties on partial objects.
                        assigned = vars(self.app) if self.app is not None else {}
                        backend = assigned.get("_backend")
                        client = assigned.get("client")
                        # LiteTUI.store is a lazy property backed by _store.
                        store = assigned.get("_store")
                        if backend is not None:
                            stack.callback(self.cleanup, "backend", backend.shutdown)
                        if client is not None:
                            stack.push_async_callback(self.cleanup_async, "client", client.close)
                        if store is not None:
                            stack.callback(self.cleanup, "store", store.release)
            finally:
                # Run even if construction, test body or a cleanup callback
                # raises. Exception chaining retains any preceding failures.
                if global_before is not None:
                    assert settings.settings_path().read_bytes() == global_before
                if agent_before is not None:
                    assert (home / "settings.json").read_bytes() == agent_before
                assert not (root / ".convos").exists()
                assert not self.denied, f"forbidden attempts (including swallowed errors): {self.denied}"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    return ConstructionGate(tmp_path, monkeypatch)


@pytest_asyncio.fixture
async def constructed(gate):
    async with gate.open() as value:
        yield value


@pytest.mark.asyncio
async def test_owned_constructor_preserves_authority_and_real_agent_lease(constructed, tmp_path):
    app, session, calls = constructed
    assert calls == ["hooks-substituted"]
    assert type(app.store) is ConversationRepository
    assert app.store.agent_session is session
    assert (app.settings.backend, app.settings.default_model, app.settings.thinking_level) == (
        "lmstudio", "fixture-model", "high")
    assert (app.seat.name, app.seat.agent_id) == (NAME, AID)
    assert app.backend.name == "lmstudio"
    assert not app.seat.registered
    assert app.convo_dir == session.conversation_directory(app.convo_id)
    assert app._read_store_file("memory.md", 1000) == "synthetic persistent memory"
    assert app.store.pending and not app.store.owned
    # create_fresh reserves the address directory; no transcript is written yet.
    assert list(tmp_path.rglob("convo.jsonl")) == []
    with pytest.raises(agent_ownership.OwnershipError):
        agent_launch_context.acquire(tmp_path, NAME)
    app.store.release()
    assert session.authority.agent_id == AID
    session.release()
    with agent_launch_context.acquire(tmp_path, NAME) as recovered:
        assert recovered.authority.agent_id == AID
    with pytest.raises(agent_ownership.OwnershipError):
        session.conversation_directory(app.convo_id)


@pytest.mark.asyncio
async def test_real_materialization_and_conversation_lease_remain_separate(constructed, tmp_path):
    app, session, _ = constructed
    app._materialise_convo()
    assert not app.store.pending and app.store.owned
    assert app.store.persist_error is None
    path = app.store.convo_path
    assert path is not None and path.is_file()
    meta, messages = ConversationRepository.read(path)
    assert (meta["id"], meta["agent_id"], meta["agent_name"]) == (app.convo_id, AID, NAME)
    assert messages == app.conversation
    assert any(message["role"] == "system" and message["content"] for message in messages)
    before = path.read_bytes()
    app._materialise_convo()
    assert app.store.persist_error is None
    assert path.read_bytes() == before
    assert list(tmp_path.rglob("convo.jsonl")) == [path]
    rival = ConversationRepository(agent_session=session)
    try:
        with pytest.raises(shared_state.OwnershipError):
            rival.adopt(path, app.convo_id)
        assert path.read_bytes() == before
        app.store.release()
        assert session.authority.agent_id == AID
        rival.adopt(path, app.convo_id)
        assert rival.owned
        assert path.read_bytes() == before
        with pytest.raises(agent_ownership.OwnershipError):
            agent_launch_context.acquire(tmp_path, NAME)
    finally:
        rival.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [False, True])
async def test_real_owned_startup_registration_uses_one_transport_attempt(
    constructed, tmp_path, monkeypatch, succeeds,
):
    app, session, _ = constructed
    app._materialise_convo()
    attempts = []
    entered, release = threading.Event(), threading.Event()
    second_entered = asyncio.Event()

    def register():
        attempts.append("transport")
        entered.set()
        if not release.wait(5):
            raise RuntimeError("fixture registration release timed out")
        app.seat.registered = succeeds
        app.seat.error = "" if succeeds else "synthetic refusal"
        return succeeds

    async def await_transport():
        while not entered.is_set():
            await asyncio.sleep(0.001)

    async def second_startup():
        second_entered.set()
        return await app._register_owned_startup()

    monkeypatch.setattr(app.seat, "register", register)
    tasks = [asyncio.create_task(app._register_owned_startup())]
    try:
        await asyncio.wait_for(await_transport(), timeout=2)
        tasks.append(asyncio.create_task(second_startup()))
        await asyncio.wait_for(second_entered.wait(), timeout=2)
        assert not tasks[1].done()
        assert attempts == ["transport"]
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert results == [succeeds] * 2
    assert attempts == ["transport"]
    if succeeds:
        assert app.store.owned
        assert session.authority.agent_id == AID
        with pytest.raises(agent_ownership.OwnershipError):
            agent_launch_context.acquire(tmp_path, NAME)
    else:
        assert not app.store.owned
        assert "owned-registration-failed" in app._cli_launch_error
        assert not await app._register_owned_startup()
        assert attempts == ["transport"]
        with pytest.raises(agent_ownership.OwnershipError):
            _ = session.authority
        with agent_launch_context.acquire(tmp_path, NAME) as recovered:
            rival = ConversationRepository(agent_session=recovered)
            try:
                rival.adopt(app.store.convo_path, app.convo_id)
                assert rival.owned
            finally:
                rival.release()


class InjectedFailure(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_partial_constructor_failure_closes_real_resources(gate, monkeypatch):
    def fail(_app):
        raise InjectedFailure("constructor")

    monkeypatch.setattr(LiteTUI, "_load_system_prompt", fail)
    with pytest.raises(InjectedFailure, match="constructor"):
        async with gate.open():
            pytest.fail("constructor unexpectedly returned")
    assert gate.app.client.is_closed()
    assert gate.cleaned == ["store", "client", "backend", "session"]
    with agent_launch_context.acquire(gate.root, NAME) as recovered:
        assert recovered.authority.agent_id == AID


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["store", "client", "backend"])
async def test_cleanup_failure_does_not_skip_other_resources(gate, monkeypatch, resource):
    with pytest.raises(InjectedFailure, match=resource):
        async with gate.open() as (app, _session, _calls):
            obj, method = {
                "store": (app.store, "release"),
                "client": (app.client, "close"),
                "backend": (app.backend, "shutdown"),
            }[resource]
            original = getattr(obj, method)

            def fail_after_cleanup():
                original()
                raise InjectedFailure(resource)

            async def fail_after_async_cleanup():
                await original()
                raise InjectedFailure(resource)

            monkeypatch.setattr(obj, method, fail_after_async_cleanup if resource == "client" else fail_after_cleanup)
    assert gate.app.client.is_closed()
    assert gate.cleaned == ["store", "client", "backend", "session"]
    with agent_launch_context.acquire(gate.root, NAME) as recovered:
        assert recovered.authority.agent_id == AID


@pytest.mark.asyncio
async def test_swallowed_forbidden_attempt_still_fails_gate_after_cleanup(gate):
    with pytest.raises(AssertionError, match="forbidden attempts"):
        async with gate.open():
            with socket.socket() as sock:
                with pytest.raises(AssertionError, match="connect_ex"):
                    sock.connect_ex(("127.0.0.1", 1))
    assert gate.denied == ["socket.connect_ex"]
    assert gate.cleaned == ["store", "client", "backend", "session"]
    assert gate.app.client.is_closed()
    with agent_launch_context.acquire(gate.root, NAME) as recovered:
        assert recovered.authority.agent_id == AID
