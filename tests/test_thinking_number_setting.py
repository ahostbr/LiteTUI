"""T1398: thinking_level / compact_thinking_level take a level name or a manual number.

Prefix (81aeebf4, 7e4c3977): "Only finite numbers >= 0 are valid, and anything else is refused at load
with the field named, never coerced. A file with today's strings has to load unchanged; write that
test. Bools aren't numbers (isinstance(True, int)), so guard against them."
"""
import json
import logging

import pytest

from litetui import settings as st


def _load(tmp_path, data):
    (tmp_path / st.SETTINGS_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    return st.load(tmp_path)


@pytest.mark.parametrize("level", ["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_a_file_with_todays_strings_loads_unchanged(tmp_path, level):
    s = _load(tmp_path, {"thinking_level": level, "compact_thinking_level": level})
    assert (s.thinking_level, s.compact_thinking_level) == (level, level)


def test_ryans_own_settings_value_loads_unchanged(tmp_path):
    """C:/Projects/LiteTUI/settings.json holds thinking_level "high" and compact_thinking_level "low"."""
    s = _load(tmp_path, {"thinking_level": "high", "compact_thinking_level": "low"})
    assert (s.thinking_level, s.compact_thinking_level) == ("high", "low")


@pytest.mark.parametrize("number", [0, 0.0, 0.1, 0.99, 60, 100])
def test_a_finite_number_at_or_above_zero_loads_and_keeps_its_type(tmp_path, number):
    s = _load(tmp_path, {"thinking_level": number})
    assert s.thinking_level == number
    assert type(s.thinking_level) is type(number), "DeepSeek's template takes an INTEGER: 60 stays 60"


@pytest.mark.parametrize("bad", [True, False, -0.1, -1, [0.1], {"x": 1}])
def test_anything_else_is_refused_with_the_field_named_and_the_default_kept(tmp_path, caplog, bad):
    with caplog.at_level(logging.WARNING, logger="litetui.settings"):
        s = _load(tmp_path, {"thinking_level": bad})
    assert s.thinking_level == st.Settings().thinking_level
    assert "thinking_level" in caplog.text


def test_a_non_finite_number_is_refused(tmp_path):
    """JSON cannot hold NaN by the standard, but Python's json writes and reads it."""
    (tmp_path / st.SETTINGS_FILENAME).write_text('{"compact_thinking_level": NaN}', encoding="utf-8")
    assert st.load(tmp_path).compact_thinking_level == "low"


def test_a_number_from_the_environment_is_a_number(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_THINKING", "0.1")
    assert st.load(tmp_path).thinking_level == 0.1
    monkeypatch.setenv("LITETUI_THINKING", "high")
    assert st.load(tmp_path).thinking_level == "high"


def _service_verdict(tmp_path, value):
    """Run SettingsService.save_patch's own validation on one thinking_level change. Validation
    runs before any write; a value that passes it fails later on the deliberately wrong revision,
    which is a different error. Returns "refused" or "accepted"."""
    from litetui.settings_service import SETTING_SPECS, SettingChange, SettingsService

    spec = SETTING_SPECS["thinking_level"]
    service = SettingsService(tmp_path)
    try:
        service.save_patch("t1398-convo", [SettingChange("thinking_level", value, spec.scope.value)],
                           {"conversation": "not-a-revision", "global": "not-a-revision"})
    except ValueError as error:
        if "thinking_level" in str(error) and ("Invalid value" in str(error) or "not a" in str(error)):
            return "refused"
        return "accepted"
    except Exception:
        return "accepted"
    return "accepted"


@pytest.mark.parametrize("value,verdict", [
    ("high", "accepted"), (0.1, "accepted"), (60, "accepted"), (0, "accepted"),
    ("banana", "refused"), (True, "refused"), (-1, "refused"), (None, "refused"),
])
def test_the_settings_service_accepts_names_and_numbers_and_refuses_the_rest(tmp_path, value, verdict):
    """The strict save path keeps the Literal guard for names (PassLink's 'banana') and adds numbers."""
    assert _service_verdict(tmp_path, value) == verdict
