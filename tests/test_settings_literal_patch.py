"""Save-time validation of Literal settings, and per-conversation isolation.

Written by PassLink (2b0b6b7c) for the editable sidecar (a patch is not
constrained by a Select); the isolation arm uses codex, which exists on main.
"""
import pytest

from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingChange, SettingsService


@pytest.mark.parametrize('key', ['thinking_level', 'compact_thinking_level'])
def test_save_patch_refuses_a_value_outside_the_literal(tmp_path, key):
    """A sidecar patch is not constrained by a Select. 'none' is the ENGINE's
    spelling (ClinePass/custom reasoning_levels); the saved vocabulary says 'off'."""
    svc = SettingsService(tmp_path)
    snap = svc.snapshot('a')
    scope = SETTING_SPECS[key].scope.value
    before = getattr(snap.saved, key)
    with pytest.raises(ValueError, match=key):
        svc.save_patch('a', [SettingChange(key, 'none', scope)], snap.revisions)
    assert getattr(svc.snapshot('a').saved, key) == before
    result = svc.save_patch('a', [SettingChange(key, 'off', scope)], snap.revisions)
    assert result.fully_saved
    assert getattr(svc.snapshot('a').saved, key) == 'off'


def test_a_backend_saved_on_one_conversation_stays_there(tmp_path):
    svc = SettingsService(tmp_path)
    a, b = svc.snapshot('a'), svc.snapshot('b')
    scope = SETTING_SPECS['backend'].scope.value
    assert svc.save_patch('a', [SettingChange('backend', 'codex', scope)], a.revisions).fully_saved
    assert svc.snapshot('a').saved.backend == 'codex'
    assert svc.snapshot('b').saved.backend == b.saved.backend
