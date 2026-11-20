"""A system line said before the chat log is attached waits for it; it does not crash the app.

`_apply_cli_args` is a worker started from `on_mount`. When it has nothing to wait for (a refused
`--model`, a thinking level the model does not have) it reports at once through `system_message`,
which mounted into `#chat-log` whether or not that widget was attached yet. Textual answers
`MountError("Can't mount widget(s) before ChatLog(id='chat-log') is mounted")`, the worker fails,
and the app goes down with it at launch.

Measured 2026-11-20 on develop b7d7c380: tests/test_approval_over_rpc.py::
test_deny_and_remember_survive_the_wire failed 2 runs in 12 with exactly that error, under the
system Python (Textual 8.0.2) and under the app's own venv (Textual 8.1.0).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _owned_seat import owned_app


def make_app():
    a = owned_app()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _lines(app) -> list[str]:
    return [str(m.render()) for m in app.query(".system-msg")]


@pytest.mark.asyncio
async def test_a_line_said_while_the_log_is_not_attached_arrives_once_it_is(monkeypatch) -> None:
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        for _ in range(6):
            await pilot.pause()
        log = a.query_one("#chat-log")
        attached = {"now": False}
        monkeypatch.setattr(type(log), "is_attached", property(lambda self: attached["now"]))
        a.system_message("said too early")               # used to raise MountError here
        await pilot.pause()
        assert not any("said too early" in line for line in _lines(a)), "it must wait, not mount blind"
        attached["now"] = True
        for _ in range(8):
            await pilot.pause()
        hits = [line for line in _lines(a) if "said too early" in line]
        assert len(hits) == 1, f"the line arrives once the log is attached, exactly once; got {hits}"


@pytest.mark.asyncio
async def test_the_wait_ends_when_the_log_never_comes(monkeypatch) -> None:
    """An app that is going down never attaches the log again: the wait is bounded, not a loop."""
    import time

    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        for _ in range(6):
            await pilot.pause()
        log = a.query_one("#chat-log")
        monkeypatch.setattr(type(log), "is_attached", property(lambda self: False))
        scheduled: list = []
        real = a.call_after_refresh
        monkeypatch.setattr(a, "call_after_refresh", lambda fn, *args: (scheduled.append(args), real(fn, *args))[1])
        a.system_message("still waiting")                 # no deadline given: it sets one and waits
        assert len(scheduled) == 1 and scheduled[0][1] == "still waiting"
        deadline = scheduled[0][2]
        assert 9.0 < deadline - time.monotonic() <= 10.0, "the wait is ten seconds from the first try"
        scheduled.clear()
        a.system_message("never shown", time.monotonic() - 1.0)   # a deadline that has passed
        assert scheduled == [], "past the deadline nothing is scheduled again"
        monkeypatch.setattr(type(log), "is_attached", property(lambda self: True))
        for _ in range(4):
            await pilot.pause()
        assert not any("never shown" in line for line in _lines(a))


@pytest.mark.asyncio
async def test_an_ordinary_line_is_shown_at_once() -> None:
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        for _ in range(6):
            await pilot.pause()
        a.system_message("plain line")
        await pilot.pause()
        assert sum("plain line" in line for line in _lines(a)) == 1
