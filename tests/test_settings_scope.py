"""Exhaustive, fail-closed persistence scope classification."""
from dataclasses import fields
import pytest
from litetui.settings import Settings
from litetui.settings_scope import SETTING_SPECS, SettingScope, validate_registry


def test_every_persisted_field_has_one_explicit_scope():
    validate_registry()
    assert set(SETTING_SPECS) == {f.name for f in fields(Settings)}


def test_execution_fields_are_conversation_owned():
    for name in ('backend', 'default_model', 'ninfer_host', 'temperature',
                 'tools_disabled', 'model_infer_overrides', 'subagent_model'):
        assert SETTING_SPECS[name].scope == SettingScope.CONVERSATION
    assert SETTING_SPECS['theme_name'].scope == SettingScope.DEVICE
    assert SETTING_SPECS['backend_chosen'].scope == SettingScope.DEFAULTS


def test_missing_mapping_fails_closed():
    broken = dict(SETTING_SPECS)
    broken.pop('ninfer_host')
    with pytest.raises(ValueError, match='ninfer_host'):
        validate_registry(broken)
