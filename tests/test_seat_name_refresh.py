"""External registration renames propagate to the running seat and footer."""
import json
from types import SimpleNamespace

from litetui import harness
from litetui.app import LiteTUI


def test_heartbeat_adopts_external_name_before_writing_presence(tmp_path, monkeypatch):
    seat = harness.Seat("abc", "OldName", "model")
    monkeypatch.delenv(harness.NO_HARNESS_ENV, raising=False)
    seat.registered = True
    agents = tmp_path / "agents"
    agents.mkdir()
    row = agents / "abc.json"
    row.write_text(json.dumps({"agent_id": "abc", "name": "NewName"}), encoding="utf-8")
    original_refresh = harness.Seat.refresh_name
    monkeypatch.setattr(harness.Seat, "refresh_name", lambda self: original_refresh(self, tmp_path))
    sent = []
    monkeypatch.setattr(harness, "_cli", lambda args, **kw: sent.append(args) or SimpleNamespace(returncode=0, stdout=""))

    assert seat.heartbeat()
    assert seat.name == "NewName"
    assert sent[0][sent[0].index("--name") + 1] == "NewName"
    row.write_text("{bad", encoding="utf-8")
    seat.heartbeat()
    assert seat.name == "NewName"


def test_refreshed_seat_name_is_in_footer(monkeypatch):
    app = LiteTUI()
    app.seat.registered = True
    app.seat.name = "NewName"
    assert "NewName" in app.ctx_label_text.plain
