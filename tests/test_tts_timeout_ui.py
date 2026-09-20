"""Voice settings coverage for the service-owned TTS timeout field."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from litetui.settings_screen import _validate_tts_timeout
from litetui.settings_ui_model import SETTINGS_SECTIONS

SCREEN = Path(__file__).resolve().parents[1] / "src" / "litetui" / "settings_screen.py"


def test_tts_timeout_is_searchable_in_voice_speak_section():
    section = next(section for section in SETTINGS_SECTIONS if section.section_id == "voice-speak")

    assert "tts_timeout" in tuple(field.name for field in section.fields)
    assert "timeout" in section.keywords


def test_tts_timeout_control_and_validation_are_wired():
    source = SCREEN.read_text(encoding="utf-8")

    assert '"tts_timeout", "TTS timeout (seconds)"' in source
    assert "_validate_tts_timeout(out.tts_timeout)" in source
    assert "input Speak toggle" in source
    assert "ONLY speak on/off" not in source

    _validate_tts_timeout(300)
    with pytest.raises(ValueError, match="tts_timeout"):
        _validate_tts_timeout(0)
    with pytest.raises(ValueError, match="tts_timeout"):
        _validate_tts_timeout(-1)


def test_tts_timeout_source_remains_valid_python():
    ast.parse(SCREEN.read_text(encoding="utf-8"))
