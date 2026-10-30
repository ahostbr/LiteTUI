"""Source sensitivity and requested/effective metadata used by native inspection."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from litetui import gpu_gate, gui_rpc
from litetui.settings import Settings
from litetui.settings_scope import SETTING_SPECS
from litetui.sidecar_settings import SECRET_FIELDS


@pytest.mark.parametrize("visible_ninfer", [False, True])
def test_settings_metadata_has_canonical_sensitivity_and_complete_values(monkeypatch, visible_ninfer):
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: visible_ninfer)
    settings = Settings()
    app = SimpleNamespace(settings=settings)
    before = deepcopy(settings)
    result = gui_rpc._settings(app)
    fields = {row["name"]: row for row in result["metadata"]}
    assert len(fields) == len(result["metadata"])
    assert fields.keys() == result["requested"].keys() == result["effective"].keys()
    assert {name for name, row in fields.items() if row["secret"]} == SECRET_FIELDS
    assert all(type(row["secret"]) is bool for row in fields.values())
    assert all(row["apply"] == SETTING_SPECS[name].apply_timing for name, row in fields.items())
    assert ("ninfer_host" in fields) is visible_ninfer
    assert result["deferred"] == {}
    assert settings == before


def test_deferred_metadata_preserves_requested_and_effective_values(monkeypatch):
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    settings = Settings()
    app = SimpleNamespace(settings=settings)
    initial = gui_rpc._settings(app)
    app._gui_effective_settings = deepcopy(initial["effective"])
    settings.lm_host = "http://synthetic.invalid:1234"
    settings.skill_roots = ["synthetic-root"]
    settings.max_tokens_chat += 1
    settings.default_context_length = 8192
    settings.strata_max_context = 16384
    result = gui_rpc._settings(app)
    assert result["deferred"] == {"lm_host": "reconnect", "skill_roots": "reconnect",
                                  "default_context_length": "reload", "strata_max_context": "restart"}
    assert result["effective"]["lm_host"] == initial["effective"]["lm_host"]
    assert result["requested"]["lm_host"] == settings.lm_host
    assert result["effective"]["max_tokens_chat"] == settings.max_tokens_chat


def test_canonical_lifecycle_modes_are_reported_without_duplicate_registries(monkeypatch):
    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    metadata = {row["name"]: row["apply"] for row in gui_rpc._settings(SimpleNamespace(settings=Settings()))["metadata"]}
    assert metadata["default_context_length"] == "reload"
    assert metadata["ninfer_max_context"] == "restart"
    assert metadata["skill_roots"] == "reconnect"
    assert metadata["temperature"] == "next_turn"
    assert metadata["theme_name"] == "immediate"
