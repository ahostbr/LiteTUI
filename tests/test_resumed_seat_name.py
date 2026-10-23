"""Registry identity wins over stale conversation names on resume."""
import json
from types import SimpleNamespace

import pytest

from _owned_seat import owned_app
from litetui import harness
from litetui.store_identity import capture


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


HOME_ID = "11111111-2222-3333-4444-555555555555"
CONVO_ID = "99999999-8888-7777-6666-555555555555"


def _saved_convo(app, saved_name):
    # _resume refuses a transcript outside the owned agent's conversations folder.
    folder = app._agent_session.conversation_directory(CONVO_ID)
    folder.mkdir(parents=True)
    path = folder / "convo.jsonl"
    meta = {"type": "meta", "v": 3, "id": CONVO_ID}
    if saved_name:
        meta["agent_name"] = saved_name
    path.write_text(
        json.dumps(meta) + "\n"
        + json.dumps({"type": "snapshot", "store_identity": capture(app), "store_injected": False,
                    "messages": [
            {"role": "system", "content": "system"}, {"role": "user", "content": "hello"}
        ]}) + "\n", encoding="utf-8")
    return path


# An owned seat's name is its home's name (a2f8e823): resuming a conversation whose
# saved agent_name differs must neither rename the seat nor ask the registry for the
# saved name. The footer keeps naming the home.
@pytest.mark.asyncio
@pytest.mark.parametrize("home,saved", [
    ("Ive-Peasant", "Ive-Peasant-next"),
    ("SpawnName", "Ive-Peasant"),
    ("Ive-Peasant", None),
])
async def test_resume_name_precedence_and_footer(tmp_path, monkeypatch, home, saved):
    monkeypatch.setenv("LITETUI_NO_HARNESS", "1")
    app = owned_app(spawn_identity=(HOME_ID, home, "worker"),
                    initial_backend="lmstudio", initial_model="a-model")
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
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
    path = _saved_convo(app, saved)
    async with app.run_test() as pilot:
        assert app._resume(path) is True
        for _ in range(10):
            await pilot.pause(0.05)
        assert claimed == []
        assert home in app.ctx_label_text.plain
        assert saved is None or saved not in app.ctx_label_text.plain
        assert app.seat.name == home


# T1125 / d6af7253: a resumed seat must not bring back a stale saved name; the
# registry (for an owned seat, the home) name is kept.
@pytest.mark.asyncio
async def test_resume_keeps_the_home_name_and_never_claims_the_saved_one(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_NO_HARNESS", "1")  # no real fleet calls in pilot
    app = owned_app()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.seat.registered = True
    app._seat_started = True
    home = app.seat.name
    claimed = []
    app.seat.claim_name = lambda name: claimed.append(name) or True
    app._render_resumed = lambda path: None
    app._sync_fleet_identity = lambda: None
    path = _saved_convo(app, "GlassGrid")
    async with app.run_test() as pilot:
        assert app._resume(path) is True
        for _ in range(10):
            await pilot.pause(0.05)
        assert claimed == [], "an owned seat must not claim a conversation's saved name"
        assert home in app.ctx_label_text.plain
        assert "GlassGrid" not in app.ctx_label_text.plain
