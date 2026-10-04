"""T0083: whoami offers a command only for a conclusively dead name holder."""
import json
import shlex

import pytest

from litetui import harness


@pytest.mark.parametrize("holder,pid,live,expect_hint", [
    ("other-seat", 123, False, True),
    ("other-seat", 123, True, False),
    ("other-seat", None, False, False),
    ("other-seat", True, False, False),
    ("other-seat", 0, False, False),
    ("own-full-agent-id", 123, False, False),
])
def test_whoami_manual_takeover_hint_is_full_own_command_only_for_dead_holder(
        tmp_path, monkeypatch, holder, pid, live, expect_hint):
    monkeypatch.delenv(harness.NO_HARNESS_ENV, raising=False)
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)
    (tmp_path / "holder.json").write_text(json.dumps({
        "agent_id": holder, "name": "Example Seat", "session_pid": pid,
    }), encoding="utf-8")
    monkeypatch.setattr(harness.router_record, "pid_is_live", lambda pid: live)

    def no_execution(*args, **kwargs):
        pytest.fail("whoami hint must never execute registration")
    monkeypatch.setattr(harness, "_cli", no_execution)
    seat = harness.Seat("own-full-agent-id", "Example Seat", "model with spaces", tier="worker")
    out = harness.run(seat, {"action": "whoami"})
    hints = [line for line in out.splitlines() if line.startswith("Manual dead-holder retry: ")]
    assert bool(hints) == expect_hint
    if expect_hint:
        assert len(hints) == 1
        command = shlex.split(hints[0].removeprefix("Manual dead-holder retry: "))
        assert command == ["liteharness", *seat._presence_argv(), "--takeover"]
        for flag, value in (("--agent-id", seat.agent_id), ("--cli", seat.cli),
                            ("--model", seat.model), ("--tier", seat.tier), ("--name", seat.name)):
            assert command[command.index(flag) + 1] == value
        assert holder not in command
    assert "--takeover" not in seat._presence_argv()
    assert not seat.registered
    assert json.loads((tmp_path / "holder.json").read_text(encoding="utf-8"))["agent_id"] == holder
