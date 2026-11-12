"""LM Studio's seat cycle for the studio tool (T0702).

The studio tool runs on a worker thread. The old resume was `asyncio.run(self.load(...))`, and
`load` goes through the App's VRAM gate, which — with another local LiteTUI live — opens a modal
or an RPC future bound to the UI loop. From the worker thread that question cannot be asked, so
the seat this tool had just unloaded did not come back. The resume puts back the weights the
suspend took out, not a second set (`second_instance.needs_vram_confirmation`'s own rule), so it
must not ask; and neither verb may depend on which thread or loop it is called from.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from litetui import seat_guard
from litetui.llm_backend import LMStudioBackend


def _backend(calls: list):
    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend._host = "http://127.0.0.1:1234"
    loaded = {"seat": 32768}

    def load(key, config=None):
        calls.append(("load", key, config))
        loaded[key] = (config or {}).get("contextLength")

    def unload(key):
        calls.append(("unload", key))
        loaded.pop(key, None)

    backend._sdk = lambda: SimpleNamespace(load=load, unload=unload)
    backend.seat_snapshot = lambda key: ({"identifier": key, "context": loaded[key], "parallel": 1,
                                          "status": "idle", "queued": 0} if key in loaded else None)

    async def gate(model):
        pytest.fail(f"the seat's own restore asked the VRAM gate about {model!r}")

    backend.vram_gate = gate
    return backend


def test_the_cycle_restores_the_seat_without_asking_the_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "crumb.json")
    calls = []
    backend = _backend(calls)
    rec = seat_guard.record("seat", backend)
    assert seat_guard.suspend(rec, backend) is None
    assert seat_guard.resume(rec, backend) is None
    assert calls == [("unload", "seat"), ("load", "seat", {"contextLength": 32768})]
    assert not seat_guard.BREADCRUMB.exists()


def test_the_verbs_work_from_inside_a_running_loop_too():
    """The old verbs answered "called from a running event loop" — a thread detail, not the
    seat's state."""
    calls = []
    backend = _backend(calls)

    async def cycle():
        rec = {"identifier": "seat", "context": 32768}
        return backend.seat_suspend(rec), backend.seat_resume(rec)

    assert asyncio.run(cycle()) == (None, None)
    assert [c[0] for c in calls] == ["unload", "load"]
