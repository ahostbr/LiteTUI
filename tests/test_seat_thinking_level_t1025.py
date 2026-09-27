"""T1025 — the seat reports the model and thinking level THIS process resolved.

The fleet floor (liteharness fleet_policy.verify_seat) reads the seat's presence
row after a spawn. That row must carry what the next request will actually use,
after resume, the T1004 pin and any launch flag, never what a file said.
No test here writes the live registry: argv is inspected, register is a fake.
"""

from __future__ import annotations

import asyncio

import pytest

from litetui import app as app_mod
from litetui import harness as harness_mod


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_presence_carries_the_thinking_level():
    seat = harness_mod.Seat(agent_id="i", name="S", model="gpt-6-sol")
    seat.thinking_level = "high"
    assert _flag(seat._presence_argv(), "--thinking-level") == "high"


def test_no_level_is_reported_as_default_not_omitted():
    seat = harness_mod.Seat(agent_id="i", name="S", model="gpt-6-sol")
    assert _flag(seat._presence_argv(), "--thinking-level") == "default"


class _App:
    def __init__(self, **attrs):
        self.seat = harness_mod.Seat(agent_id="i", name="S", model="")
        self.model_id = "gpt-5.6-sol"
        self.__dict__.update(attrs)


def test_sync_prefers_the_launch_level_the_seat_applied():
    app = _App(_cli_effective_thinking="high", _thinking_level="medium", model_id="gpt-6-sol")
    app_mod._sync_seat_resolution(app)
    assert (app.seat.model, app.seat.thinking_level) == ("gpt-6-sol", "high")


def test_sync_falls_back_to_the_resolved_level():
    app = _App(_thinking_level="medium")
    app_mod._sync_seat_resolution(app)
    assert (app.seat.model, app.seat.thinking_level) == ("gpt-5.6-sol", "medium")


class _RecordingSeat:
    def __init__(self):
        self.model = None
        self.thinking_level = None
        self.name = "S"
        self.agent_id = "i"
        self.registered = False
        self.error = f"disabled by {harness_mod.NO_HARNESS_ENV}"
        self.seen = None

    def register(self) -> bool:
        self.seen = (self.model, self.thinking_level)
        return False


class _MonitorApp:
    def __init__(self):
        self.seat = _RecordingSeat()
        self.model_id = "gpt-5.6-sol"          # what the convo/pin said at boot
        self._thinking_level = "medium"
        self._cli_args_done = asyncio.Event()
        self._seat_started = False

    def _system(self, text: str) -> None:
        pass


@pytest.mark.asyncio
async def test_registration_waits_for_the_launch_flags(monkeypatch):
    """Registering before _apply_cli_args finished would report the pinned
    5.6-sol/medium for a seat launched --model gpt-6-sol --thinking-level high,
    and the spawner would kill a seat that is in fact at the floor."""
    monkeypatch.setattr(app_mod, "_INBOX_SETTLE_S", 0)
    app = _MonitorApp()
    task = asyncio.ensure_future(app_mod.LiteTUI._inbox_monitor.__wrapped__(app))
    await asyncio.sleep(0.05)
    assert app.seat.seen is None, "registered before the launch flags were applied"
    app.model_id, app._cli_effective_thinking = "gpt-6-sol", "high"
    app._cli_args_done.set()
    await asyncio.wait_for(task, 5)
    assert app.seat.seen == ("gpt-6-sol", "high")
