"""Isolated GUI and local transport arms: never touch real seats/models/registry."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from litetui import gui_rpc, local_rpc
from litetui.settings import Settings


class Backend:
    name = "ninfer"
    remote = False
    models = {"weights": {}}
    def __init__(self):
        self.calls = []
    async def load(self, key, **kw):
        self.calls.append(("load", key, kw))
    async def unload(self, key):
        self.calls.append(("unload", key))
    def reasoning_levels(self, key):
        return ["low", "high"]
    def engine_status(self):
        return "fake status"
    async def start_engine(self):
        self.calls.append(("start",))
        return "fake started"
    def begin_stop(self):
        self.calls.append(("begin",))
        return True
    def stop_engine(self):
        self.calls.append(("stop",))
        return "fake stopped"
    def end_stop(self):
        self.calls.append(("end",))


def app():
    backend = Backend()
    a = SimpleNamespace(backend=backend, model_id="weights", settings=Settings(),
                        thinking_level=None, _chat_running=lambda: False,
                        _rpc_model_state=lambda: {"model": "weights"},
                        _fetch_ctx_window=lambda: None, connect=lambda: None)
    return a


@pytest.mark.asyncio
async def test_shared_load_ctx_context_unload_owner():
    a = app()
    await gui_rpc.async_dispatch(a, {"type": "gui.models.load", "slug": "weights", "ctx": 8192})
    await gui_rpc.async_dispatch(a, {"type": "gui.context.set", "context": 4096})
    await gui_rpc.async_dispatch(a, {"type": "gui.models.unload", "slug": "weights"})
    assert a.backend.calls == [("load", "weights", {"ctx": 8192}), ("load", "weights", {"ctx": 4096}), ("unload", "weights")]
    for operation, field in (("gui.models.load", "ctx"), ("gui.context.set", "context")):
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(a, {"type": operation, field: 0})


@pytest.mark.asyncio
async def test_context_failure_never_success_ack():
    a = app()
    async def refused(*a, **kw):
        raise ValueError("load refused")
    a.backend.load = refused
    with pytest.raises(ValueError, match="load refused"):
        await gui_rpc.async_dispatch(a, {"type": "gui.context.set", "context": 4096})
    assert not a._gui_management_busy


@pytest.mark.asyncio
async def test_engine_all_three_shared_owner_arms():
    a = app()
    assert (await gui_rpc.async_dispatch(a, {"type": "gui.engine.status"}))["status"] == "fake status"
    assert (await gui_rpc.async_dispatch(a, {"type": "gui.engine.start"}))["result"] == "fake started"
    assert (await gui_rpc.async_dispatch(a, {"type": "gui.engine.stop"}))["result"] == "fake stopped"
    assert a.backend.calls == [("start",), ("begin",), ("stop",), ("end",)]
    a.backend.name = "claude"
    # "Strata" is in the refusal on every machine; NInfer only on an RTX 5090 (test_gpu_gate).
    with pytest.raises(ValueError, match="Strata"):
        await gui_rpc.async_dispatch(a, {"type": "gui.engine.start"})


@pytest.mark.asyncio
async def test_backend_reuses_reconnect_owner(monkeypatch):
    from litetui import llm_backend, settings_runtime
    monkeypatch.delenv("LITETUI_BACKEND", raising=False)
    a = app()
    events = []
    monkeypatch.setattr(settings_runtime, "persist_or_raise", lambda *args: events.append("persist"))
    monkeypatch.setattr(llm_backend, "make_backend", lambda settings: SimpleNamespace(name=settings.backend))
    class Worker:
        async def wait(self):
            a._gui_connection_success = True
            events.append("connected")
    a.connect = lambda: Worker()
    result = await gui_rpc.async_dispatch(a, {"type": "gui.backend.set", "name": "claude"})
    assert result["connected"] and a.backend.name == "claude" and events == ["persist", "connected"]


@pytest.mark.asyncio
async def test_thinking_same_capability_owner_and_haiku_high_rejection():
    a = app()
    result = await gui_rpc.async_dispatch(a, {"type": "gui.thinking.set", "level": "high"})
    assert result["level"] == "high"
    from litetui.claude_backend import ClaudeBackend
    backend = SimpleNamespace(name="claude", models={"haiku": {}}, reasoning_levels=None)
    backend.reasoning_levels = lambda key: ClaudeBackend.reasoning_levels(backend, key)
    a.backend, a.model_id = backend, "haiku"
    with pytest.raises(ValueError, match="not supported"):
        await gui_rpc.async_dispatch(a, {"type": "gui.thinking.set", "level": "high"})
    a.backend = Backend()
    caps = await gui_rpc.async_dispatch(a, {"type": "gui.models.capabilities", "backend": "claude", "model": "haiku"})
    assert caps["thinking"]["levels"] == ["default"]
    with pytest.raises(ValueError, match="absent"):
        await gui_rpc.async_dispatch(a, {"type": "gui.models.capabilities", "backend": "claude", "model": "not-real"})


class Writer:
    def __init__(self, peer="127.0.0.1"):
        self.frames = []
        self.peer = peer
    def get_extra_info(self, name):
        return (self.peer, 1234)
    def write(self, raw):
        self.frames.append(json.loads(raw))
    async def drain(self):
        pass
    def close(self):
        pass
    async def wait_closed(self):
        pass


def reader(*frames):
    r = asyncio.StreamReader(limit=local_rpc.MAX_FRAME + 1)
    for frame in frames:
        r.feed_data((json.dumps(frame) + "\n").encode())
    r.feed_eof()
    return r


def transport(a):
    t = local_rpc.LocalRpc(a)
    t.endpoint = {"agent_id": "fake-seat", "pid": os.getpid(), "nonce": "fake-nonce"}
    return t


@pytest.mark.asyncio
async def test_correlated_concurrent_clients_use_same_loop_not_stdout(monkeypatch):
    t = transport(app())
    loop = asyncio.get_running_loop()
    calls = []
    async def dispatch(a, cmd):
        assert asyncio.get_running_loop() is loop
        calls.append(cmd)
        await asyncio.sleep(0)
        return {"accepted": cmd["id"]}
    monkeypatch.setattr(gui_rpc, "async_dispatch", dispatch)
    writers = [Writer(), Writer()]
    auth = {"agent_id": "fake-seat", "nonce": "fake-nonce", "token": t.secret}
    await asyncio.gather(*(t._client(reader(auth, {"type": "gui.state", "id": str(i)}), w)
                           for i, w in enumerate(writers)))
    assert {c["id"] for c in calls} == {"0", "1"}
    for i, w in enumerate(writers):
        assert w.frames[-1] == {"type": "response", "id": str(i), "ok": True, "result": {"accepted": str(i)}}
        assert t.secret not in json.dumps(w.frames)
    assert not hasattr(t.app, "_gui_rpc_enabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["token", "unicode-token", "agent", "nonce", "remote", "operation", "id", "oversize"])
async def test_unauthorized_or_malformed_never_dispatches(monkeypatch, bad):
    t = transport(app())
    monkeypatch.setattr(gui_rpc, "async_dispatch", lambda *args: pytest.fail("must not dispatch"))
    auth = {"agent_id": "fake-seat", "nonce": "fake-nonce", "token": t.secret}
    cmd = {"type": "gui.state", "id": "bounded"}
    if bad == "token": auth["token"] = "wrong"
    if bad == "unicode-token": auth["token"] = "\u2603"
    if bad == "agent": auth["agent_id"] = "other"
    if bad == "nonce": auth["nonce"] = "old"
    if bad == "operation": cmd["type"] = "gui.host_tools.register"
    if bad == "id": cmd["id"] = "x" * 129
    if bad == "oversize": cmd["extra"] = "x" * local_rpc.MAX_FRAME
    w = Writer("192.0.2.1" if bad == "remote" else "127.0.0.1")
    await t._client(reader(auth, cmd), w)
    assert not w.frames or w.frames[-1].get("ok") is False


@pytest.mark.asyncio
async def test_busy_refuses_same_owner():
    a = app()
    a._chat_running = lambda: True
    with pytest.raises(ValueError, match="active"):
        await gui_rpc.async_dispatch(a, {"type": "gui.thinking.set", "level": "high"})


@pytest.mark.asyncio
async def test_acl_failure_publishes_no_endpoint(monkeypatch, tmp_path):
    from litetui import harness, paths
    a = app()
    a.seat = SimpleNamespace(registered=True, agent_id="fake-seat")
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    monkeypatch.setattr(local_rpc, "_private_file", lambda *args: (_ for _ in ()).throw(PermissionError("ACL refused")))
    monkeypatch.setattr(local_rpc, "_presence", lambda *args, **kw: pytest.fail("must not advertise"))
    t = local_rpc.LocalRpc(a)
    with pytest.raises(PermissionError):
        await t.start()
    assert t.server is None and t.endpoint is None


def test_private_token_created_owner_only_and_exclusive(tmp_path):
    path = tmp_path / "token.json"
    local_rpc._private_file(path, {"token": "fake secret"})
    assert json.loads(path.read_text()) == {"token": "fake secret"}
    with pytest.raises(OSError):
        local_rpc._private_file(path, {"token": "must not overwrite"})
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_isolated_loopback_start_rotation_close(monkeypatch, tmp_path):
    from litetui import harness, paths
    a = app()
    a.seat = SimpleNamespace(registered=True, agent_id="fake-seat")
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    advertisements = []
    monkeypatch.setattr(local_rpc, "_presence", lambda endpoint, **kw: advertisements.append((dict(endpoint), kw)) or True)
    monkeypatch.setattr(gui_rpc, "async_dispatch", lambda *args: async_result({"fake": True}))
    t = local_rpc.LocalRpc(a)
    await t.start()
    old_secret, old_endpoint = t.secret, dict(t.endpoint)
    r, w = await asyncio.open_connection("127.0.0.1", t.endpoint["port"])
    w.write((json.dumps({"agent_id": "fake-seat", "nonce": t.endpoint["nonce"], "token": t.secret}) + "\n").encode())
    await w.drain()
    assert json.loads(await r.readline())["type"] == "authenticated"
    w.write(b'{"type":"gui.state","id":"roundtrip"}\n')
    await w.drain()
    assert json.loads(await r.readline())["id"] == "roundtrip"
    w.close()
    await w.wait_closed()
    await t.close()
    assert not t.token_path.exists() and advertisements[-1][1] == {"remove": True}
    new = local_rpc.LocalRpc(a)
    await new.start()
    assert new.secret != old_secret and new.endpoint["nonce"] != old_endpoint["nonce"]
    await new.close()


async def async_result(value):
    return value
