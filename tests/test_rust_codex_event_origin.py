"""Native producer provenance through the real adapter; external I/O is synthetic."""
from __future__ import annotations

import asyncio
from functools import partial
from types import SimpleNamespace as NS

import pytest
from test_codex_app_server import Server

from litetui import model_transport, rpc
from litetui.app import LiteTUI
from litetui.codex_app_server import AppServerTransport
from litetui.event_origin import EventOrigin

pytestmark = pytest.mark.asyncio


def host(monkeypatch):
    records = []
    monkeypatch.setattr(rpc, "rpc_emit", records.append)
    app = NS(convo_id="session-A", _gui_operation_id="operation-A",
             _hook_turn_id="hook-A", _hook_source="rpc", _rpc=True,
             _gui_rpc_enabled=True, tools_enabled=False, backend=NS(models={}),
             conversation=[])
    app._rpc_emit = partial(LiteTUI._rpc_emit, app)
    return app, records


def captured(name="A"):
    return EventOrigin(session_id=f"session-{name}", operation_id=f"operation-{name}",
                       hook_id=f"hook-{name}", source="rpc").scheduled()


def envelopes(records):
    return [row for row in records if row.get("type") == "gui.event"
            and row["event"]["type"] == "native_turn_started"]


@pytest.mark.parametrize("boundary", ["start", "account/read", "turn/start"])
async def test_native_turn_origin_survives_awaited_ambient_drift(monkeypatch, boundary):
    app, records = host(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    class HeldServer(Server):
        async def start(self):
            if boundary == "start":
                entered.set()
                await release.wait()

        async def request(self, method, params):
            if method == boundary:
                entered.set()
                await release.wait()
            return await super().request(method, params)

    server = HeldServer()
    transport = AppServerTransport(server, app)
    origin = captured()
    stream = await transport.create(origin=origin, model="fixture-model",
                                    messages=[{"role": "user", "content": "hello"}],
                                    stream=True)
    task = asyncio.create_task(model_transport.collect(stream))
    await asyncio.wait_for(entered.wait(), 2)
    app.convo_id, app._gui_operation_id = "session-B", "operation-B"
    app._hook_turn_id, app._hook_source = "hook-B", "queued"
    release.set()
    await asyncio.wait_for(task, 2)
    emission, = envelopes(records)
    assert {key: emission.get(key) for key in origin.envelope_fields()} == origin.envelope_fields()
    assert all("origin" not in params for _, params in server.requests)


async def test_correction_retry_preserves_outer_origin_and_new_execution_rebinds(monkeypatch):
    app, records = host(monkeypatch)
    server = Server()
    transport = AppServerTransport(server, app)
    first, following = captured(), captured("B")
    for origin in (first, first, following):
        stream = await transport.create(origin=origin, model="fixture-model",
                                        messages=[{"role": "user", "content": "hello"}],
                                        stream=True)
        await model_transport.collect(stream)
    emissions = envelopes(records)
    assert len(emissions) == 3
    assert [row["event"]["turnId"] for row in emissions] == ["1", "2", "3"]
    assert [{key: row.get(key) for key in first.envelope_fields()} for row in emissions] == [
        first.envelope_fields(), first.envelope_fields(), following.envelope_fields()]


async def test_cancelled_native_start_does_not_emit_or_leak_origin_to_next_execution(monkeypatch):
    app, records = host(monkeypatch)
    entered = asyncio.Event()

    class HeldServer(Server):
        hold = True

        async def request(self, method, params):
            if method == "turn/start" and self.hold:
                entered.set()
                await asyncio.Event().wait()
            return await super().request(method, params)

    server = HeldServer()
    transport = AppServerTransport(server, app)
    stream = await transport.create(origin=captured(), model="fixture-model",
                                    messages=[{"role": "user", "content": "hello"}],
                                    stream=True)
    task = asyncio.create_task(model_transport.collect(stream))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert envelopes(records) == []
    server.hold = False
    following = captured("B")
    stream = await transport.create(origin=following, model="fixture-model",
                                    messages=[{"role": "user", "content": "next"}],
                                    stream=True)
    await model_transport.collect(stream)
    emission, = envelopes(records)
    assert {key: emission.get(key) for key in following.envelope_fields()} == following.envelope_fields()


async def test_openai_transport_consumes_origin_before_provider_call():
    seen = []

    async def create(**kwargs):
        seen.append(kwargs)
        return "synthetic response"

    client = NS(chat=NS(completions=NS(create=create)))
    response = await model_transport.OpenAITransport(client).create(
        origin=captured(), model="fixture-model", messages=[], stream=True)
    assert response == "synthetic response"
    assert seen == [{"model": "fixture-model", "messages": [], "stream": True}]


async def test_oauth_transport_consumes_origin_before_wire_builder(monkeypatch):
    class ReachedBuilder(Exception):
        pass

    def build(kwargs):
        assert "origin" not in kwargs
        raise ReachedBuilder

    monkeypatch.setattr(model_transport, "read_credentials", lambda *_: NS(access="fake", account_id="fake"))
    monkeypatch.setattr(model_transport, "codex_request", build)
    transport = model_transport.OAuthTransport("codex")
    with pytest.raises(ReachedBuilder):
        await transport.create(origin=captured(), model="fixture-model", messages=[])
