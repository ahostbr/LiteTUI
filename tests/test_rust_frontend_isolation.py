"""Python write-attempt sensitivity for the retained frontend isolation gate.

This exercises real owned construction/storage and its existing durable-denial
recorder, without mounting a frontend or launching a provider/device/child.
The scoped wrappers use the existing performance audit's file-event classifier
but protect ONLY a disposable sibling legacy stand-in. They stay armed through
owned teardown and are then restored; no permanent sys.addaudithook is installed.
This is an in-process Python tripwire, NOT OS-wide monitoring or a sandbox.
Pre-opened descriptors, native extensions and external processes are outside its
scope. Real-child root refusal is independently covered by Rust live_process.
"""

from __future__ import annotations

import builtins
import io
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_rust_owned_construction import AID, NAME, ConstructionGate

from litetui import agent_launch_context

pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]
SENTINEL = b'{"legacy":"unchanged disposable stand-in"}\n'


def canonical(path):
    resolved = str(Path(path).resolve())
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    elif resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    return Path(resolved)


class ProtectedWriteAttempts:
    """Adapt actual Python filesystem calls to the existing denial recorder."""

    def __init__(self, gate, protected):
        self.gate = gate
        self.protected = canonical(protected)

    def observe(self, event, args):
        paths = []
        if event == "open" and not isinstance(args[0], int):
            if args[2] & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            ):
                paths.append(args[0])
        elif event in {"os.mkdir", "os.remove", "os.rmdir"}:
            paths.append(args[0])
        elif event in {"os.rename", "os.replace"}:
            paths.extend(args[:2])
        for path in paths:
            target = canonical(os.fsdecode(path))
            if target.is_relative_to(self.protected):
                self.gate.forbid(f"protected-write:{event}:{target}")()

    def wrap_open(self, original, *, raw=False):
        def checked(*args, **kwargs):
            path = args[0] if args else kwargs["path" if raw else "file"]
            mode = (
                args[1]
                if len(args) > 1
                else kwargs.get("flags" if raw else "mode", 0 if raw else "r")
            )
            flags = (
                mode
                if raw
                else (os.O_WRONLY if any(char in mode for char in "wax+") else 0)
            )
            self.observe("open", (path, None, flags))
            return original(*args, **kwargs)

        return checked

    def wrap_path(self, original, event):
        def checked(*args, **kwargs):
            if event in {"os.rename", "os.replace"}:
                paths = [
                    args[0] if args else kwargs["src"],
                    args[1] if len(args) > 1 else kwargs["dst"],
                ]
            else:
                paths = [args[0] if args else kwargs["path"]]
            self.observe(event, paths)
            return original(*args, **kwargs)

        return checked

    @contextmanager
    def armed(self, monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(builtins, "open", self.wrap_open(builtins.open))
            patch.setattr(io, "open", self.wrap_open(io.open))
            patch.setattr(os, "open", self.wrap_open(os.open, raw=True))
            for name, event in (
                ("mkdir", "os.mkdir"),
                ("remove", "os.remove"),
                ("unlink", "os.remove"),
                ("rmdir", "os.rmdir"),
                ("rename", "os.rename"),
                ("replace", "os.replace"),
            ):
                patch.setattr(os, name, self.wrap_path(getattr(os, name), event))
            yield


@pytest.fixture
def isolated_gate(tmp_path, monkeypatch):
    # A sibling, not a protected subtree of the allowed owned DataRoot.
    with tempfile.TemporaryDirectory(
        prefix="rust-private-legacy-", dir=tmp_path.parent
    ) as directory:
        legacy = Path(directory)
        sentinel = legacy / "settings.json"
        sentinel.write_bytes(SENTINEL)
        gate = ConstructionGate(tmp_path, monkeypatch)
        guard = ProtectedWriteAttempts(gate, legacy)
        yield gate, guard, sentinel
        assert sentinel.read_bytes() == SENTINEL


def assert_owned_cleanup(gate):
    assert gate.cleaned == ["store", "client", "backend", "session"]
    assert gate.app.client.is_closed()
    with agent_launch_context.acquire(gate.root, NAME) as recovered:
        assert recovered.authority.agent_id == AID


async def test_owned_writes_and_protected_reads_pass_without_denials(
    isolated_gate, monkeypatch
):
    gate, guard, sentinel = isolated_gate
    with guard.armed(monkeypatch):
        async with gate.open():
            owned = gate.root / "allowed.txt"
            owned.write_text("owned", encoding="utf-8")
            with builtins.open(owned, "a", encoding="utf-8") as handle:
                handle.write(" append")
            descriptor = os.open(owned, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(descriptor, b" raw")
            finally:
                os.close(descriptor)
            moved = gate.root / "allowed-moved.txt"
            os.replace(owned, moved)
            assert moved.read_text(encoding="utf-8") == "owned append raw"
            moved.unlink()
            assert sentinel.read_bytes() == SENTINEL
            with builtins.open(sentinel, "rb") as handle:
                assert handle.read() == SENTINEL
            descriptor = os.open(sentinel, os.O_RDONLY)
            try:
                assert os.read(descriptor, len(SENTINEL) + 1) == SENTINEL
            finally:
                os.close(descriptor)
    assert gate.denied == []
    assert_owned_cleanup(gate)


def attempt_write(sentinel, route):
    if route == "pathlib":
        sentinel.write_bytes(b"MUTATED")
    elif route == "builtin":
        with builtins.open(sentinel, "wb") as handle:
            handle.write(b"MUTATED")
    else:
        descriptor = os.open(sentinel, os.O_WRONLY | os.O_TRUNC)
        try:
            os.write(descriptor, b"MUTATED")
        finally:
            os.close(descriptor)


@pytest.mark.parametrize("route", ["pathlib", "builtin", "os.open"])
async def test_swallowed_write_attempt_fails_after_owned_cleanup(
    isolated_gate, monkeypatch, route
):
    gate, guard, sentinel = isolated_gate
    swallowed = []
    with (
        guard.armed(monkeypatch),
        pytest.raises(AssertionError, match="forbidden attempts"),
    ):
        async with gate.open():
            try:
                attempt_write(sentinel, route)
            except AssertionError as error:
                swallowed.append(str(error))
            # These assertions all pass even after the deliberately swallowed
            # error. Only the durable attempt ledger makes scope exit fail.
            assert sentinel.read_bytes() == SENTINEL
            (gate.root / "continued-after-denial").write_text(
                "continued", encoding="utf-8"
            )
    assert len(swallowed) == 1 and "protected-write:open:" in swallowed[0]
    assert gate.denied == [f"protected-write:open:{canonical(sentinel)}"]
    assert (gate.root / "continued-after-denial").read_text(
        encoding="utf-8"
    ) == "continued"
    assert_owned_cleanup(gate)


async def test_propagated_write_denial_still_completes_owned_cleanup(
    isolated_gate, monkeypatch
):
    gate, guard, sentinel = isolated_gate
    propagated = []
    with (
        guard.armed(monkeypatch),
        pytest.raises(AssertionError, match="forbidden attempts"),
    ):
        async with gate.open():
            try:
                sentinel.write_text("MUTATED", encoding="utf-8")
            except AssertionError as error:
                propagated.append(str(error))
                raise
    assert len(propagated) == 1 and "protected-write:open:" in propagated[0]
    assert gate.denied == [f"protected-write:open:{canonical(sentinel)}"]
    assert_owned_cleanup(gate)


@pytest.mark.parametrize("operation", ["replace", "remove", "mkdir"])
async def test_other_protected_mutation_attempts_are_recorded_before_effect(
    isolated_gate, monkeypatch, operation
):
    gate, guard, sentinel = isolated_gate
    expected_event = {
        "replace": "os.replace",
        "remove": "os.remove",
        "mkdir": "os.mkdir",
    }[operation]
    destination = (
        sentinel.parent / "new-directory" if operation == "mkdir" else sentinel
    )
    with (
        guard.armed(monkeypatch),
        pytest.raises(AssertionError, match="forbidden attempts"),
    ):
        async with gate.open():
            candidate = gate.root / "candidate"
            candidate.write_bytes(b"MUTATED")
            try:
                if operation == "replace":
                    os.replace(candidate, sentinel)
                elif operation == "remove":
                    sentinel.unlink()
                else:
                    destination.mkdir()
            except AssertionError:
                pass
            assert candidate.read_bytes() == b"MUTATED"
            assert sentinel.read_bytes() == SENTINEL
            assert not (sentinel.parent / "new-directory").exists()
    assert gate.denied == [f"protected-write:{expected_event}:{canonical(destination)}"]
    assert_owned_cleanup(gate)


async def test_swallowed_write_during_teardown_still_fails_after_all_cleanup(
    isolated_gate, monkeypatch
):
    gate, guard, sentinel = isolated_gate
    shutdowns = []
    with (
        guard.armed(monkeypatch),
        pytest.raises(AssertionError, match="forbidden attempts"),
    ):
        async with gate.open() as (app, _session, _calls):
            original = app.backend.shutdown

            def shutdown_with_swallowed_attempt():
                original()
                shutdowns.append("backend closed")
                try:
                    sentinel.write_bytes(b"MUTATED DURING CLEANUP")
                except AssertionError:
                    shutdowns.append("write refused")

            monkeypatch.setattr(
                app.backend, "shutdown", shutdown_with_swallowed_attempt
            )
    assert shutdowns == ["backend closed", "write refused"]
    assert gate.denied == [f"protected-write:open:{canonical(sentinel)}"]
    assert_owned_cleanup(gate)
