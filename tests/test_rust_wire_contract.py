"""P0 wire evidence: real protocol functions, no app construction or live child.

scripts/capture_rust_wire_contract.py exports synthetic inputs and actual
serialized outputs into a unique, pre-created evidence directory. These are in-process
captures, NOT subprocess/startup, provider, threading or full-parity evidence.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import io
import json
import os
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from litetui import gui_rpc, rpc, settings, tool_approval
from litetui.app import LiteTUI


class Wire(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1

    def records(self):
        text = self.getvalue()
        assert not text or text.endswith("\n")
        return [json.loads(line) for line in text.splitlines()]


class Host:
    """Synthetic host state and UI/loop/worker boundaries; no Textual lifecycle."""

    def __init__(self):
        self.callbacks = []
        self.coroutines = []
        self.exited = False
        self._rpc = True
        self._gui_rpc_enabled = False
        self._gui_operation_id = "fixture-turn-command"
        self.convo_id = "fixture-session"
        self.settings = settings.Settings()
        self._rpc_emit = MethodType(LiteTUI._rpc_emit, self)

    def call_from_thread(self, callback, *args):
        self.callbacks.append((callback, args))

    def run_worker(self, coroutine, **_kwargs):
        self.coroutines.append(coroutine)

    def exit(self):
        self.exited = True

    def drain_callbacks(self):
        while self.callbacks:
            callback, args = self.callbacks.pop(0)
            callback(*args)

    async def drain_workers(self):
        while self.coroutines:
            await self.coroutines.pop(0)


@pytest.fixture
def host():
    value = Host()
    try:
        yield value
    finally:
        pending = list(value.coroutines)
        for coroutine in pending:
            coroutine.close()
        value.coroutines.clear()
        assert not pending, "management coroutine was not drained"
        assert not value.callbacks, "loop callbacks were not drained"


@pytest.fixture
def wire(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("wire contract harness attempted app construction or an external process/network call")

    # These tripwires are not an OS sandbox. The harness never mounts the app.
    import socket
    import subprocess

    monkeypatch.setattr(LiteTUI, "__init__", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    output = Wire()
    monkeypatch.setattr(rpc, "_real_stdout", output)
    yield output
    assert output.flushes == len(output.records())


def record_case(name, inputs, wire, *, boundary):
    """Optional evidence export AFTER assertions; every supplied value is synthetic."""
    destination = os.environ.get("RUST_WIRE_EVIDENCE_DIR")
    if not destination:
        return
    root = Path(__file__).resolve().parents[1]
    target = Path(destination)
    assert target.is_absolute() and target.is_dir(), "capture runner must provision the directory before pytest"
    input_text = json.dumps(inputs, indent=2) + "\n"
    output_text = wire.getvalue()
    sources = ["src/litetui/rpc.py", "src/litetui/gui_rpc.py", "src/litetui/app.py",
               "src/litetui/tool_approval.py", "src/litetui/settings.py", "src/litetui/paths.py",
               "src/litetui/shared_state.py", "tests/test_rust_wire_contract.py", "tests/conftest.py",
               "tests/_isolated_home.py", "scripts/capture_rust_wire_contract.py"]
    manifest = {
        "format_version": 1,
        "run_id": target.name,
        "provenance_scope": "selected disk sources and runtime identity, not a complete dependency lock",
        "runtime": {"python": sys.version.split()[0], "platform": sys.platform,
                    "packages": {name: importlib.metadata.version(name) for name in ("pytest", "pytest-asyncio", "textual")}},
        "loaded_wire_modules": {module.__name__: str(Path(module.__file__).resolve().relative_to(root))
                                for module in (rpc, gui_rpc)},
        "input_sha256": hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
        "output_sha256": hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
        "case": name,
        "evidence_kind": "in-process-production-wire-functions",
        "input_origin": "synthetic test constants only; no live data imported",
        "boundary": boundary,
        "not_proven": ["CLI startup", "fd ownership", "thread scheduling", "OS pipe behavior",
                       "provider/tool execution", "isolation of a real child", "Rust consumption"],
        "source_sha256": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sources},
        "output_records": len(wire.records()),
    }
    (target / f"{name}.input.json").write_text(input_text, encoding="utf-8", newline="\n")
    (target / f"{name}.output.jsonl").write_text(output_text, encoding="utf-8", newline="\n")
    (target / f"{name}.manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def test_reader_framing_errors_continue_and_eof_requests_exit(wire, monkeypatch, host):
    lines = [' ', '{broken', '[]', '7', '{}', '{"type":"fixture.unknown","id":"fixture-command"}']
    monkeypatch.setattr(rpc, "_real_stdin", io.StringIO("\n".join(lines) + "\n"))
    rpc._reader_loop(host)
    assert not host.exited  # EOF schedules, rather than directly performs, exit.
    host.drain_callbacks()
    assert host.exited
    assert wire.records() == [
        {"type": "error", "error": "invalid JSON"},
        *[{"type": "error", "error": "missing 'type' field"}] * 3,
        {"type": "response", "ok": False, "id": "fixture-command",
         "error": "unknown command: fixture.unknown"},
    ]
    record_case("reader-framing", lines, wire, boundary="finite stdin/stdout; FIFO callback drain; exit spy")


@pytest.mark.asyncio
async def test_gui_negotiation_is_correlated_and_wrapping_is_not_deduplication(wire, host):
    before = {"type": "fixture_event", "text": "before negotiation"}
    host._rpc_emit(before)
    hello = {"type": "gui.hello", "id": "fixture-hello", "protocol_version": 1}
    rpc._dispatch(host, hello)
    await host.drain_workers()
    response = wire.records()[1]
    assert response["id"] == "fixture-hello" and response["ok"] is True
    assert response["result"]["protocol_version"] == 1
    assert "gui.prompt.submit" in response["result"]["operations"]
    event = {"type": "fixture_event", "text": 'line one\n雪 "quoted" \\ tail'}
    host._rpc_emit(event)
    host._rpc_emit(event)  # Producer does not collapse identical consecutive events.
    wrapped = {"type": "gui.event", "session_id": "fixture-session",
               "operation_id": "fixture-turn-command", "event": event}
    assert wire.records() == [before, response, event, wrapped, event, wrapped]
    assert gui_rpc.OPERATION_CONTEXT.get() is None
    record_case("negotiated-mirrors", [before, hello, event, event], wire,
                boundary="real async GUI dispatcher and app emitter; explicit coroutine drain; synthetic host state")


@pytest.mark.asyncio
async def test_bad_protocol_is_a_correlated_error_and_does_not_enable_wrapping(wire, host):
    command = {"type": "gui.hello", "id": "fixture-bad-version", "protocol_version": 99}
    rpc._dispatch(host, command)
    await host.drain_workers()
    assert wire.records() == [{"type": "response", "ok": False, "id": "fixture-bad-version",
                               "error": "Unsupported GUI protocol version; expected 1"}]
    assert not host._gui_rpc_enabled
    record_case("unsupported-protocol", [command], wire, boundary="real management error wrapper; no app constructed")


def test_usage_and_approval_details_cache_without_changing_wire_payload(wire, host):
    host._gui_rpc_enabled = True
    events = [{"type": "usage", "usage": {"input_tokens": 4, "output_tokens": 2}},
              {"type": "tool_approval_requested", "id": "fixture-approval", "tool": "fixture_write",
               "input": {"target": "fixture.txt"}, "profile": "interactive", "why": "synthetic fixture",
               "timeout_s": 30.0}]
    for event in events:
        host._rpc_emit(event)
    assert host._gui_usage == events[0]["usage"]
    assert host._gui_approval_details == {"fixture-approval": events[1]}
    assert wire.records()[::2] == events
    assert [row["event"] for row in wire.records()[1::2]] == events
    record_case("cached-event-mirrors", events, wire, boundary="real emitter; synthetic state, no approval creation or tool")


@pytest.mark.asyncio
async def test_real_approval_resolver_rejects_malformed_stale_and_duplicate_decisions(wire, request):
    future = asyncio.get_running_loop().create_future()
    request.addfinalizer(future.cancel)
    host = SimpleNamespace(_approval_waiters={"fixture-approval": future})
    commands = [
        {"type": "approve", "id": "fixture-malformed", "approval_id": "fixture-approval", "allow": "false"},
        {"type": "approve", "id": "fixture-stale", "approval_id": "not-pending", "allow": True},
        {"type": "approve", "id": "fixture-deny", "approval_id": "fixture-approval", "allow": False},
        {"type": "approve", "id": "fixture-late", "approval_id": "fixture-approval", "allow": True},
    ]
    for command in commands[:2]:
        rpc._dispatch(host, command)
        assert not future.done()
    rpc._dispatch(host, commands[2])
    assert future.result() == tool_approval.DENIED
    rpc._dispatch(host, commands[3])
    assert future.result() == tool_approval.DENIED
    rows = wire.records()
    assert [row["id"] for row in rows] == [command["id"] for command in commands]
    assert [row["ok"] for row in rows] == [False, False, True, False]
    assert rows[2]["result"] == {"approved": "fixture-approval", "allow": False, "id_key": "approval_id"}
    record_case("approval-denial", commands, wire,
                boundary="actual dispatcher/resolver and pending future; no authority record, policy evaluation, tool or app startup")


def test_operation_context_takes_precedence_and_is_restored(wire, host):
    host._gui_rpc_enabled = True
    event = {"type": "fixture_event", "text": "correlation probe"}
    token = gui_rpc.OPERATION_CONTEXT.set("fixture-management-command")
    try:
        host._rpc_emit(event)
    finally:
        gui_rpc.OPERATION_CONTEXT.reset(token)
    host._rpc_emit(event)
    assert [row["operation_id"] for row in wire.records()[1::2]] == [
        "fixture-management-command", "fixture-turn-command"]
    record_case("operation-context", [event, event], wire, boundary="real emitter; explicitly scoped ContextVar and synthetic turn id")


def test_non_rpc_emitter_is_silent(wire, host):
    host._rpc = False
    host._rpc_emit({"type": "fixture_event", "text": "must not reach wire"})
    assert wire.records() == []


@pytest.mark.parametrize("wrapped", [False, True])
def test_real_submit_refusal_precedes_accepted_ack_without_starting_a_turn(wire, wrapped, host):
    host._gui_rpc_enabled = wrapped
    host._gui_operation_id = None
    host.pending_image = None
    host._submit_text = MethodType(LiteTUI._submit_text, host)
    host._refuse_submit = MethodType(LiteTUI._refuse_submit, host)
    acquired = []
    host.store = SimpleNamespace(acquire=lambda: acquired.append(True))
    command = {"type": "prompt", "id": "fixture-refused-prompt", "message": "   "}
    rpc._dispatch(host, command)
    refusal = {"type": "submit_refused", "reason": "empty", "detail": "nothing to send"}
    expected = [refusal]
    if wrapped:
        expected.append({"type": "gui.event", "session_id": "fixture-session",
                         "operation_id": None, "event": refusal})
    expected.append({"type": "response", "ok": True, "id": "fixture-refused-prompt",
                     "result": {"turn": "accepted"}})
    assert acquired == [True]
    assert host._gui_next_operation_id is None
    assert wire.records() == expected
    record_case(f"refusal-before-ack-{'gui' if wrapped else 'raw'}", [command], wire,
                boundary="real legacy prompt dispatcher and empty-submit/refusal path; lease-acquisition spy; no app or turn")


def test_real_terminal_event_preserves_null_rates_without_an_invented_turn_id(wire, host):
    host._gui_rpc_enabled = True
    painted = []
    host._paint_now = lambda: painted.append(host._now_turn_active_at)
    LiteTUI._emit_turn_end(host, "cancelled", None)
    event = {"type": "turn_end", "stopReason": "cancelled", "tps": None, "tpsSource": None}
    assert painted == [None]
    assert wire.records() == [event, {"type": "gui.event", "session_id": "fixture-session",
                                     "operation_id": "fixture-turn-command", "event": event}]
    record_case("terminal-null-rates", [{"helper": "_emit_turn_end", "stop_reason": "cancelled", "tps": None}],
                wire, boundary="real terminal helper and emitter; paint spy; not a real turn or cancellation")


def test_serializer_string_fallback_is_visible_not_a_typed_protocol_guarantee(wire):
    rpc.rpc_emit({"type": "fixture_event", "path": Path("fixture.txt"), "nothing": None, "allowed": False})
    assert wire.records() == [{"type": "fixture_event", "path": "fixture.txt", "nothing": None, "allowed": False}]


@pytest.mark.parametrize("at_flush", [False, True])
def test_broken_stdout_is_suppressed_not_reported_as_delivery(monkeypatch, at_flush):
    class BrokenWire:
        def write(self, _text):
            if not at_flush:
                raise BrokenPipeError("synthetic closed pipe")

        def flush(self):
            raise OSError("synthetic flush failure")

    monkeypatch.setattr(rpc, "_real_stdout", BrokenWire())
    assert rpc.rpc_emit({"type": "fixture_event"}) is None


@pytest.mark.parametrize("kind", [None, 0, False, [], {}])
def test_present_non_string_type_returns_error_and_reader_continues(wire, monkeypatch, kind, host):
    commands = [{"type": kind, "id": "fixture-invalid-type"},
                {"type": "fixture.unknown", "id": "fixture-next"}]
    monkeypatch.setattr(rpc, "_real_stdin", io.StringIO("".join(json.dumps(cmd) + "\n" for cmd in commands)))
    rpc._reader_loop(host)
    host.drain_callbacks()
    assert host.exited
    assert wire.records() == [
        {"type": "response", "ok": False, "id": "fixture-invalid-type", "error": "command type must be a string"},
        {"type": "response", "ok": False, "id": "fixture-next", "error": "unknown command: fixture.unknown"},
    ]
