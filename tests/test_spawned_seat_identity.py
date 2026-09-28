from __future__ import annotations

import os
from unittest.mock import patch

from litetui import harness


KEYS = (
    "LITETUI_SPAWN_IDENTITY",
    "LITEHARNESS_AGENT_ID",
    "LITEHARNESS_AGENT_NAME",
    "LITEHARNESS_TIER",
    "LITETUI_SEAT_NAME",
)


def test_spawn_env_is_adopted_exactly_and_consumed():
    env = {
        "LITETUI_SPAWN_IDENTITY": "1",
        "LITEHARNESS_AGENT_ID": "11111111-2222-3333-4444-555555555555",
        "LITEHARNESS_AGENT_NAME": "T1015Probe",
        "LITEHARNESS_TIER": "reviewer",
        "LITETUI_SEAT_NAME": "legacy-fallback",
    }
    with patch.dict(os.environ, env, clear=False):
        assert harness.spawned_seat_identity() == (
            env["LITEHARNESS_AGENT_ID"], "T1015Probe", "reviewer"
        )
        assert all(key not in os.environ for key in KEYS)


def test_inherited_harness_identity_without_spawn_marker_is_ignored():
    env = {
        "LITEHARNESS_AGENT_ID": "11111111-2222-3333-4444-555555555555",
        "LITEHARNESS_AGENT_NAME": "ParentSeat",
        "LITEHARNESS_TIER": "reviewer",
    }
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("LITETUI_SPAWN_IDENTITY", None)
        agent_id, name, tier = harness.spawned_seat_identity("ConfiguredName")
    assert agent_id == harness.process_agent_id()
    assert name == "ConfiguredName"
    assert tier == "worker"


def test_absent_env_preserves_process_id_name_and_worker_defaults():
    with patch.dict(os.environ, {}, clear=False):
        for key in KEYS:
            os.environ.pop(key, None)
        agent_id, name, tier = harness.spawned_seat_identity()
    assert agent_id == harness.process_agent_id()
    assert name == "LiteTUI"
    assert tier == "worker"


def test_existing_litetui_name_env_remains_a_spawn_fallback():
    with patch.dict(os.environ, {
        "LITETUI_SPAWN_IDENTITY": "1",
        "LITETUI_SEAT_NAME": "ExistingName",
    }, clear=False):
        for key in ("LITEHARNESS_AGENT_ID", "LITEHARNESS_AGENT_NAME", "LITEHARNESS_TIER"):
            os.environ.pop(key, None)
        assert harness.spawned_seat_identity()[1] == "ExistingName"


def test_app_constructs_seat_from_marked_identity_and_consumes_it(monkeypatch):
    from litetui import app as app_mod

    env = {
        "LITETUI_SPAWN_IDENTITY": "1",
        "LITEHARNESS_AGENT_ID": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "LITEHARNESS_AGENT_NAME": "AppPathProbe",
        "LITEHARNESS_TIER": "thinker",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = app_mod.LiteTUI()
    assert (app.seat.agent_id, app.seat.name, app.seat.tier) == (
        env["LITEHARNESS_AGENT_ID"], "AppPathProbe", "thinker"
    )
    assert all(key not in os.environ for key in KEYS)
    assert app._launch_seat_name == "AppPathProbe"


def test_invalid_spawn_tier_logs_and_falls_back(capsys):
    with patch.dict(os.environ, {
        "LITETUI_SPAWN_IDENTITY": "1",
        "LITEHARNESS_TIER": "emperor",
    }, clear=False):
        for key in ("LITEHARNESS_AGENT_ID", "LITEHARNESS_AGENT_NAME", "LITETUI_SEAT_NAME"):
            os.environ.pop(key, None)
        assert harness.spawned_seat_identity()[2] == "worker"
    assert "invalid LITEHARNESS_TIER 'emperor'; using worker" in capsys.readouterr().err
