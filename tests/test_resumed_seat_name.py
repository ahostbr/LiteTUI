"""Registry identity wins over stale conversation names on resume."""
import json
from types import SimpleNamespace

import pytest

from litetui import harness
from litetui.app import LiteTUI


def test_registry_name_reads_only_own_id_and_valid_name(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    seat = harness.Seat("current-id", "OldName", "model")
    agents = tmp_path / "agents"
    agents.mkdir()
    row = agents / "current-id.json"
    row.write_text(json.dumps({"agent_id": "current-id", "name": " CorrectName "}), encoding="utf-8")
    assert seat.registry_name(tmp_path) == "CorrectName"
    row.write_text(json.dumps({"agent_id": "another-id", "name": "Wrong"}), encoding="utf-8")
    assert seat.registry_name(tmp_path) is None
    row.unlink()
    assert seat.registry_name(tmp_path) is None


def test_claim_name_uses_current_agent_id_and_adopts_registry_result(monkeypatch):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    seat = harness.Seat("current-id", "SolidBolt", "model")
    seat.registered = True
    calls = []
    monkeypatch.setattr(harness, "_cli", lambda argv, **kw: (calls.append(argv),
                        SimpleNamespace(returncode=0, stdout="Registered agent current-id: cli=litetui, name=GlassGrid"))[1])
    assert seat.claim_name("GlassGrid")
    assert calls[0][calls[0].index("--agent-id") + 1] == "current-id"
    assert calls[0][calls[0].index("--name") + 1] == "GlassGrid"
    assert "--takeover" not in calls[0], "T1027: never evict a live holder"
    assert seat.name == "GlassGrid"


def test_claim_name_displays_registry_assigned_name(monkeypatch):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    seat = harness.Seat("current-id", "SolidBolt", "model")
    seat.registered = True
    monkeypatch.setattr(harness, "_cli", lambda argv, **kw:
                        SimpleNamespace(returncode=0, stdout="Registered agent current-id: cli=litetui, name=GlassGrid-2"))
    assert seat.claim_name("GlassGrid")
    assert seat.name == "GlassGrid-2"


def test_claim_name_failure_does_not_claim_identity(monkeypatch):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    seat = harness.Seat("current-id", "SolidBolt", "model")
    seat.registered = True
    monkeypatch.setattr(harness, "_cli", lambda argv, **kw:
                        SimpleNamespace(returncode=1, stdout="", stderr="refused"))
    assert not seat.claim_name("GlassGrid")
    assert seat.name == "SolidBolt"


@pytest.mark.asyncio
@pytest.mark.parametrize("registry,launch,expected,claims", [
    ("Ive-Peasant", "", "Ive-Peasant", []),
    ("Ive-Peasant", "SpawnName", "SpawnName", ["SpawnName"]),
    (None, "", "Ive-Peasant-next", ["Ive-Peasant-next"]),
])
async def test_resume_name_precedence_and_footer(tmp_path, monkeypatch, registry, launch,
                                                 expected, claims):
    monkeypatch.setenv("LITETUI_NO_HARNESS", "1")
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.seat.name = "DefaultSeat"
    app.seat.registered = True
    app._seat_started = True
    app._launch_seat_name = launch
    app.seat.registry_name = lambda: registry
    claimed = []

    def claim(name):
        claimed.append(name)
        app.seat.name = name
        return True

    app.seat.claim_name = claim
    app._render_resumed = lambda path: None
    app._sync_fleet_identity = lambda: None
    folder = tmp_path / "convo"
    folder.mkdir()
    path = folder / "convo.jsonl"
    path.write_text(
        json.dumps({"type": "meta", "v": 3, "id": "convo", "agent_name": "Ive-Peasant-next"}) + "\n"
        + json.dumps({"type": "snapshot", "messages": [
            {"role": "system", "content": "system"}, {"role": "user", "content": "hello"}
        ]}) + "\n", encoding="utf-8")
    async with app.run_test() as pilot:
        app._resume(path)
        for _ in range(30):
            await pilot.pause(0.05)
            if claimed == claims and expected in app.ctx_label_text.plain:
                break
        assert claimed == claims
        assert expected in app.ctx_label_text.plain
        assert app.seat.name == expected
    assert "Ive-Peasant-next" in path.read_text(encoding="utf-8").splitlines()[0]


@pytest.mark.asyncio
async def test_resume_claims_saved_name_and_repaints_footer(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_NO_HARNESS", "1")  # no real fleet calls in pilot
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.seat.name = "SolidBolt"
    app.seat.registered = True
    app._seat_started = True
    claimed = []

    def claim(name):
        claimed.append(name)
        app.seat.name = name
        return True

    app.seat.claim_name = claim
    app._render_resumed = lambda path: None
    app._sync_fleet_identity = lambda: None
    folder = tmp_path / "convo"
    folder.mkdir()
    path = folder / "convo.jsonl"
    path.write_text(
        '{"type":"meta","v":3,"id":"convo","agent_name":"GlassGrid"}\n'
        '{"type":"snapshot","messages":[{"role":"system","content":"system"},'
        '{"role":"user","content":"hello"}]}\n', encoding="utf-8")
    async with app.run_test() as pilot:
        app._resume(path)
        for _ in range(30):
            if claimed:
                break
            await pilot.pause(0.05)
        assert claimed == ["GlassGrid"]
        assert "GlassGrid" in app.ctx_label_text.plain
