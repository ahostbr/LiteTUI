"""Private real loopback listener and source-owned Glassbox lifecycle."""

from __future__ import annotations

import socket
import threading
import time

import pytest
from test_rust_runtime_controls import app as owner_app
from test_rust_runtime_controls import armed as owner_armed
from test_rust_runtime_controls import switch_app as owner_switch

from litetui import background_input as work
from litetui import gui_rpc, native_glassbox
from litetui.plugins import glassbox_plugin as gb


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_stop_joins_idle_sse_and_incomplete_http_handlers_with_exact_listener_identity():
    server = gb.Server()
    clients = []
    try:
        assert server.start(0).startswith("serving")
        first = server.instance
        listener, worker = server._httpd, server._thread
        assert server.running and server.port == listener.server_address[1]
        for request in (
            b"GET /events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n",
            b"GET /events",
        ):
            connection = socket.create_connection((gb.HOST, server.port), timeout=1)
            clients.append(connection)
            connection.sendall(request)
        wait_until(lambda: len(listener.clients) == 2 and len(listener.hub._subs) == 1)
        handlers = tuple(listener.clients)
        started = time.monotonic()
        assert server.stop().startswith("stopped")
        assert time.monotonic() - started < gb.STOP_SECONDS
        assert not worker.is_alive() and all(not t.is_alive() for t in handlers)
        assert not listener.hub._subs and server.snapshot() == {
            "state": "stopped",
            "port": None,
            "listener": None,
        }
        assert server.start(0).startswith("serving") and server.instance != first
    finally:
        server.stop()
        for connection in clients:
            connection.close()


def test_bind_failure_and_foreign_owner_leave_accurate_state():
    owner, other = object(), object()
    server = gb.Server()
    busy = socket.socket()
    try:
        busy.bind((gb.HOST, 0))
        busy.listen(1)
        assert server.start(busy.getsockname()[1], owner=owner).startswith("[error]")
        assert server.snapshot() == {"state": "stopped", "port": None, "listener": None}
        assert server.start(0, owner=owner).startswith("serving")
        snapshot = server.snapshot()
        assert server.stop(owner=other).startswith("[error]")
        assert server.snapshot() == snapshot
        assert server.start(0, owner=other).startswith("[error]")
        assert server.snapshot() == snapshot
    finally:
        server.stop(owner=owner)
        busy.close()


def test_failed_join_retains_handles_until_the_same_worker_retires(monkeypatch):
    server = gb.Server()
    release = threading.Event()
    try:
        assert server.start(0).startswith("serving")
        listener, identity = server._httpd, server.instance
        stuck = threading.Thread(target=release.wait, daemon=True)
        connection, peer = socket.socketpair()
        with listener.clients_lock:
            listener.clients[stuck] = connection
            stuck.start()
        monkeypatch.setattr(gb, "STOP_SECONDS", 0.05)
        assert server.stop().startswith("[error]")
        assert server._httpd is listener and server.instance == identity
        assert server.snapshot() == {
            "state": "stopping",
            "port": None,
            "listener": identity,
        }
        assert server.start(0).startswith("[error]")
        release.set()
        stuck.join(1)
        assert server.stop().startswith("stopped")
        peer.close()
    finally:
        release.set()
        server.stop()


app, armed, switch_app = owner_app, owner_armed, owner_switch


def command(app, operation, **fields):
    bridge = work.bridge(app)
    return {
        "type": native_glassbox.PREFIX + operation,
        "id": "glassbox-" + operation,
        "version": 1,
        **bridge.identity,
        "work_instance": bridge.instance,
        **fields,
    }


@pytest.mark.asyncio
async def test_reviewed_handler_bind_failure_readback_stale_and_owned_shutdown(
    armed, monkeypatch
):
    app, _ = armed
    server = gb.Server()
    messages = []
    monkeypatch.setattr(app, "system_message", messages.append)
    monkeypatch.setattr(gb, "SERVER", server)
    monkeypatch.setattr(gb, "_ENABLED", False)
    busy = socket.socket()
    busy.bind((gb.HOST, 0))
    busy.listen(1)
    monkeypatch.setattr(gb, "DEFAULT_PORT", busy.getsockname()[1])
    try:
        snapshot = gui_rpc.dispatch(app, command(app, "snapshot"))
        assert snapshot["state"] == "stopped" and not snapshot["recording"]
        assert snapshot["record_path"].endswith("glassbox.jsonl")
        review = gui_rpc.dispatch(app, command(app, "review", action="start"))
        assert not server.running and not gb._ENABLED
        receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
        assert (
            receipt["status"] == "partial"
            and receipt["recording"]
            and receipt["port"] is None
        )
        readback = gui_rpc.dispatch(
            app,
            command(
                app, "readback", token=review["token"], apply_id=receipt["apply_id"]
            ),
        )
        assert (
            readback["kind"] == "glassbox_verified" and readback["status"] == "partial"
        )
        busy.close()
        review = gui_rpc.dispatch(app, command(app, "review", action="start"))
        receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
        readback = gui_rpc.dispatch(
            app,
            command(
                app, "readback", token=review["token"], apply_id=receipt["apply_id"]
            ),
        )
        assert (
            readback["status"] == "applied" and server.owner is app and server.running
        )
        listener, thread = server._httpd, server._thread
        review = gui_rpc.dispatch(app, command(app, "review", action="stop"))
        gb._cmd_glassbox(app, "glassbox", "stop")
        gb._cmd_glassbox(app, "glassbox", "start")
        with pytest.raises(ValueError, match="changed"):
            gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
        assert not thread.is_alive() and server._httpd is not listener
        assert server._httpd.server_address == (gb.HOST, server.port)
        connection = socket.socket()
        connection.settimeout(1)
        connection.connect(server._httpd.server_address)
        try:
            connection.sendall(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
            listener, thread = server._httpd, server._thread
            wait_until(lambda: bool(listener.hub._subs))
            await native_glassbox.settle_before_shutdown(app)
            assert not gb._ENABLED and not server.running and not thread.is_alive()
            assert not listener.hub._subs and all(
                not t.is_alive() for t in listener.clients
            )
        finally:
            connection.close()
    finally:
        busy.close()
        server.stop(owner=app)


@pytest.mark.asyncio
async def test_unreviewed_foreign_handler_owner_path_and_link_are_refused(
    armed, monkeypatch, tmp_path
):
    app, _ = armed
    monkeypatch.setattr(gb, "SERVER", gb.Server())
    monkeypatch.setattr(gb, "_ENABLED", False)
    with pytest.raises(ValueError, match="exact"):
        gui_rpc.dispatch(app, command(app, "apply", token="a" * 64))
    cmd = command(app, "review", action="start")
    with pytest.raises(ValueError, match="another version"):
        gui_rpc.dispatch(app, {**cmd, "work_instance": "foreign"})
    from dataclasses import replace

    entry = app.plugins.commands["glassbox"]
    review = gui_rpc.dispatch(app, command(app, "review", action="start"))
    app.plugins.commands["glassbox"] = replace(entry)
    try:
        with pytest.raises(ValueError, match="changed"):
            gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    finally:
        app.plugins.commands["glassbox"] = entry
    original = gb._RECORDER.path
    monkeypatch.setattr(gb._RECORDER, "path", tmp_path / "outside.jsonl")
    with pytest.raises(ValueError, match="owned artifacts"):
        gui_rpc.dispatch(app, cmd)
    monkeypatch.setattr(gb._RECORDER, "path", original)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_text("preserve", encoding="utf-8")
    import os

    os.link(original, tmp_path / "linked-record.jsonl")
    with pytest.raises(ValueError, match="single regular"):
        gui_rpc.dispatch(app, cmd)
    assert (
        not gb._ENABLED and not gb.SERVER.running and original.read_text() == "preserve"
    )


@pytest.mark.asyncio
async def test_unowned_listener_refused_and_owned_listener_survives_global_replacement(
    armed, monkeypatch
):
    app, _ = armed
    server = gb.Server()
    monkeypatch.setattr(gb, "SERVER", server)
    monkeypatch.setattr(app, "system_message", lambda _message: None)
    try:
        assert server.start(0).startswith("serving")
        with pytest.raises(ValueError, match="retained listener"):
            gui_rpc.dispatch(app, command(app, "review", action="stop"))
        server.stop()
        busy = socket.socket()
        busy.bind((gb.HOST, 0))
        monkeypatch.setattr(gb, "DEFAULT_PORT", busy.getsockname()[1])
        busy.close()
        gb._cmd_glassbox(app, "glassbox", "start")
        worker = server._thread
        assert server.owner is app
        foreign = gb.Server()
        monkeypatch.setattr(gb, "SERVER", foreign)
        with pytest.raises(ValueError, match="retained listener"):
            gui_rpc.dispatch(app, command(app, "review", action="start"))
        await native_glassbox.settle_before_shutdown(app)
        assert not worker.is_alive() and server._httpd is None
        assert foreign._httpd is None
    finally:
        server.stop(owner=app)


@pytest.mark.asyncio
async def test_handler_announcement_failure_preserves_verified_effects_and_one_use_review(
    armed, monkeypatch
):
    app, _ = armed
    server = gb.Server()
    monkeypatch.setattr(gb, "SERVER", server)
    monkeypatch.setattr(gb, "_ENABLED", False)
    reservation = socket.socket()
    reservation.bind((gb.HOST, 0))
    monkeypatch.setattr(gb, "DEFAULT_PORT", reservation.getsockname()[1])
    reservation.close()

    def fail_announcement(_message):
        raise RuntimeError("controlled announcement failure")

    monkeypatch.setattr(app, "system_message", fail_announcement)
    try:
        review = gui_rpc.dispatch(app, command(app, "review", action="start"))
        receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
        assert receipt["status"] == "failed" and receipt["recording"]
        assert receipt["state"] == "running" and server.owner is app
        readback = gui_rpc.dispatch(
            app,
            command(
                app, "readback", token=review["token"], apply_id=receipt["apply_id"]
            ),
        )
        assert (
            readback["status"] == "failed" and readback["listener"] == server.instance
        )
        with pytest.raises(ValueError, match="changed"):
            gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
        await native_glassbox.settle_before_shutdown(app)
        assert server._httpd is None and not gb._ENABLED
    finally:
        server.stop(owner=app)


@pytest.mark.asyncio
async def test_retained_glassbox_worker_prevents_app_storage_release(monkeypatch):
    from types import SimpleNamespace

    from litetui import conversation_shutdown, native_engine_shutdown
    from litetui.app import LiteTUI

    host = SimpleNamespace(events=[])

    async def settled(_host):
        pass

    async def retained(_host):
        raise RuntimeError("Glassbox retains unfinished workers")

    async def release():
        host.events.append("released")

    host._release_owned_storage = release
    monkeypatch.setattr(conversation_shutdown, "settle_before_shutdown", settled)
    monkeypatch.setattr(native_engine_shutdown, "settle_before_shutdown", settled)
    monkeypatch.setattr(native_glassbox, "settle_before_shutdown", retained)
    with pytest.raises(RuntimeError, match="retains unfinished"):
        await LiteTUI._shutdown(host)
    assert host._gui_quitting and host.events == []


def test_listener_caps_accepted_workers_and_joins_every_retained_client(monkeypatch):
    server = gb.Server()
    clients = []
    monkeypatch.setattr(gb, "MAX_CLIENTS", 2)
    try:
        assert server.start(0).startswith("serving")
        listener = server._httpd
        for _ in range(2):
            connection = socket.create_connection((gb.HOST, server.port), timeout=1)
            clients.append(connection)
            connection.sendall(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
        wait_until(lambda: len(listener.subscribers) == 2)
        excess = socket.create_connection((gb.HOST, server.port), timeout=1)
        clients.append(excess)
        assert excess.recv(1) == b""
        assert len(listener.clients) == 2
        assert server.stop().startswith("stopped")
        assert all(not worker.is_alive() for worker in listener.clients)
        assert not listener.subscribers
    finally:
        server.stop()
        for connection in clients:
            connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["review", "apply"])
@pytest.mark.parametrize("held", ["active", "mutation"])
async def test_other_active_or_settling_owner_refuses_new_effects(
    armed, monkeypatch, stage, held
):
    app, _ = armed
    monkeypatch.setattr(gb, "SERVER", gb.Server())
    monkeypatch.setattr(gb, "_ENABLED", False)
    request = command(app, "review", action="start")
    if stage == "apply":
        review = gui_rpc.dispatch(app, request)
        request = command(app, "apply", token=review["token"])
    if held == "active":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    else:
        monkeypatch.setattr(
            native_glassbox.controls,
            "mutation_hold",
            lambda _app: "controlled operation settling",
        )
    with pytest.raises(ValueError, match="active|settling"):
        gui_rpc.dispatch(app, request)
    assert not gb._ENABLED and gb.SERVER._httpd is None
    assert getattr(work.bridge(app), "glassbox_review", None) is None


def test_accept_worker_start_failure_closes_listener_and_keeps_stopped_state(
    monkeypatch,
):
    server = gb.Server()
    listeners = []
    make_listener = gb._OwnedHTTPServer

    def create_listener(address):
        listener = make_listener(address)
        listeners.append(listener)
        return listener

    def fail_start(_thread):
        raise RuntimeError("controlled start failure")

    monkeypatch.setattr(gb, "_OwnedHTTPServer", create_listener)
    monkeypatch.setattr(threading.Thread, "start", fail_start)
    assert server.start(0, owner=object()).startswith("[error]")
    assert len(listeners) == 1 and listeners[0].socket.fileno() == -1
    assert server._httpd is None and server._thread is None and server.owner is None
    assert server.snapshot() == {"state": "stopped", "port": None, "listener": None}
