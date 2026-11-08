"""LiteTUI passes only its marker-captured spawner to register/heartbeat."""
from litetui.harness import Seat


def test_presence_argv_names_captured_spawner_on_each_write():
    seat = Seat("child", "Child", "gpt-6-sol")
    assert "--spawned-by" not in seat._presence_argv()
    seat.spawned_by = "leader-id"
    args = seat._presence_argv()
    assert args[args.index("--spawned-by") + 1] == "leader-id"
    assert seat._presence_argv() == args


# ── T0085: the spawner's pty-* id is joined to the seat's own presence row ──────

import json
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import harness as harness_mod


def _row(root, agent_id, **fields):
    (root / "agents").mkdir(exist_ok=True)
    (root / "agents" / f"{agent_id}.json").write_text(
        json.dumps({"agent_id": agent_id, **fields}), encoding="utf-8")


def _own(root):
    return json.loads((root / "agents" / "own-id.json").read_text(encoding="utf-8"))


def _seat(root, provisional="pty-1-2", spawner_row=True):
    _row(root, "own-id", name="Child", tier="worker", thinking_level="high")
    if spawner_row:
        _row(root, "pty-1-2", spawn_mode="pty")  # what `spawn --pty` pre-creates
    seat = harness_mod.Seat("own-id", "Child", "gpt-6-sol")
    seat.provisional_id = provisional
    return seat


def test_provisional_id_is_merged_into_the_seats_own_row_and_nothing_else_changes(tmp_path):
    assert _seat(tmp_path)._record_provisional(tmp_path) is True
    assert _own(tmp_path) == {"agent_id": "own-id", "name": "Child", "tier": "worker",
                              "thinking_level": "high", "provisional_id": "pty-1-2"}


@pytest.mark.parametrize("provisional,spawner_row", [
    ("pty-1-2", False),   # no spawner-made row: a nested litetui that inherited an id claims nothing
    ("own-id", True),     # never its own id
    ("", True), (None, True),
    (".." + chr(92) + "x", True), ("a/b", True), ("pty 1", True),   # not a daemon id: never a path
])
def test_provisional_id_is_recorded_only_when_it_is_a_spawners_row(tmp_path, provisional, spawner_row):
    seat = _seat(tmp_path, provisional, spawner_row)
    assert seat._record_provisional(tmp_path) is False
    assert "provisional_id" not in _own(tmp_path)


def test_provisional_id_never_creates_a_row(tmp_path):
    _row(tmp_path, "pty-1-2", spawn_mode="pty")
    seat = harness_mod.Seat("own-id", "Child", "gpt-6-sol")
    seat.provisional_id = "pty-1-2"
    assert seat._record_provisional(tmp_path) is False
    assert not (tmp_path / "agents" / "own-id.json").exists()


def test_merge_helpers_refuse_another_seats_row_and_keep_recap_working(tmp_path):
    _row(tmp_path, "own-id", name="Child")
    path = tmp_path / "agents" / "own-id.json"
    assert harness_mod.merge_provisional_presence(path, "someone-else", "pty-1-2") is False
    assert harness_mod.merge_recap_presence(path, "own-id", "did the thing") is True
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row["name"] == "Child" and row["last_recap"] == "did the thing" and "last_recap_at" in row
    assert not list(path.parent.glob(".recap-*")) and not list(path.parent.glob(".prov-*"))


class _Done:
    returncode = 0
    stdout = "Registered agent own-id: cli=litetui, model=m, tier=worker, name=Child"
    stderr = ""


@pytest.mark.parametrize("call", ["register", "heartbeat"])
def test_register_and_heartbeat_both_record_the_provisional_id(monkeypatch, tmp_path, call):
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)   # the live registry is not this suite's to write
    monkeypatch.setattr(harness_mod, "_cli", lambda *_a, **_k: _Done())
    (tmp_path / ".liteharness").mkdir()
    seat = _seat(tmp_path / ".liteharness")
    seat.registered = call == "heartbeat"
    monkeypatch.setattr(seat, "refresh_name", lambda *a, **k: False)
    assert getattr(seat, call)() is True
    assert _own(tmp_path / ".liteharness")["provisional_id"] == "pty-1-2"


def test_startup_captures_the_provisional_id_and_pops_it(monkeypatch, tmp_path):
    from litetui import settings
    from litetui.agent_launch_context import ordinary

    cfg = settings.Settings(default_model="home-model")
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setenv(harness_mod.PROVISIONAL_ID_ENV, " pty-1-2 ")
    with ordinary(tmp_path, cfg) as session:
        app = app_mod.LiteTUI(agent_session=session)
        assert app.seat.provisional_id == "pty-1-2"
        assert harness_mod.PROVISIONAL_ID_ENV not in __import__("os").environ   # no child inherits it


def test_a_path_shaped_id_is_never_followed_even_when_the_file_exists(tmp_path):
    # ../evil resolves to root/evil.json, which EXISTS: only the id-shape check stops it.
    _row(tmp_path, "own-id", name="Child")
    (tmp_path / "evil.json").write_text(json.dumps({"agent_id": "evil"}), encoding="utf-8")
    seat = harness_mod.Seat("own-id", "Child", "gpt-6-sol")
    seat.provisional_id = "../evil"
    assert (tmp_path / "agents" / "../evil.json").exists()
    assert seat._record_provisional(tmp_path) is False
    assert "provisional_id" not in _own(tmp_path)


@pytest.mark.parametrize("provisional", ["other-id", "pty-", "pty-\u00e9"])
def test_only_plain_pty_ids_are_claimed_even_when_the_row_exists(tmp_path, provisional):
    seat = _seat(tmp_path, provisional)
    _row(tmp_path, provisional)
    assert seat._record_provisional(tmp_path) is False
    assert "provisional_id" not in _own(tmp_path)


def test_even_a_plain_pty_id_cannot_claim_itself(tmp_path):
    _row(tmp_path, "pty-1-2", name="Child")
    seat = harness_mod.Seat("pty-1-2", "Child", "home-model")
    seat.provisional_id = "pty-1-2"
    assert seat._record_provisional(tmp_path) is False
    row = json.loads((tmp_path / "agents" / "pty-1-2.json").read_text(encoding="utf-8"))
    assert "provisional_id" not in row
