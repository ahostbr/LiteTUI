from __future__ import annotations

import os
from unittest.mock import patch

from litetui import harness


KEYS = (
    "LITEHARNESS_AGENT_ID",
    "LITEHARNESS_AGENT_NAME",
    "LITEHARNESS_TIER",
    "LITETUI_SEAT_NAME",
)


def test_spawn_env_is_adopted_exactly():
    env = {
        "LITEHARNESS_AGENT_ID": "11111111-2222-3333-4444-555555555555",
        "LITEHARNESS_AGENT_NAME": "T1015Probe",
        "LITEHARNESS_TIER": "reviewer",
    }
    with patch.dict(os.environ, env, clear=False):
        assert harness.spawned_seat_identity() == (
            env["LITEHARNESS_AGENT_ID"], "T1015Probe", "reviewer"
        )


def test_absent_env_preserves_process_id_name_and_worker_defaults():
    with patch.dict(os.environ, {}, clear=False):
        for key in KEYS:
            os.environ.pop(key, None)
        agent_id, name, tier = harness.spawned_seat_identity()
    assert agent_id == harness.process_agent_id()
    assert name == "LiteTUI"
    assert tier == "worker"


def test_existing_litetui_name_env_remains_a_fallback():
    with patch.dict(os.environ, {"LITETUI_SEAT_NAME": "ExistingName"}, clear=False):
        for key in KEYS[:-1]:
            os.environ.pop(key, None)
        assert harness.spawned_seat_identity()[1] == "ExistingName"


def test_invalid_spawn_tier_logs_and_falls_back(capsys):
    with patch.dict(os.environ, {"LITEHARNESS_TIER": "emperor"}, clear=False):
        for key in ("LITEHARNESS_AGENT_ID", "LITEHARNESS_AGENT_NAME", "LITETUI_SEAT_NAME"):
            os.environ.pop(key, None)
        assert harness.spawned_seat_identity()[2] == "worker"
    assert "invalid LITEHARNESS_TIER 'emperor'; using worker" in capsys.readouterr().err
