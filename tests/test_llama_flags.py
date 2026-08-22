"""The flag census (build gate B1): every control the Model screen offers is
backed by a flag PROVEN present in the installed llama-server's --help.

The fixture is the REAL --help of the build the wizard installed (cuda:b9360,
captured 2026-08-21). If a future build renames a flag, this file is where
the rename surfaces as a test failure instead of as a mystery load error.
"""
from __future__ import annotations

from pathlib import Path

import llm_backend

FIXTURE = Path(__file__).parent / "fixtures" / "llama_server_help_b9360.txt"


def _census() -> frozenset[str]:
    return llm_backend.parse_supported_flags(FIXTURE.read_text(encoding="utf-8"))


def test_every_mapped_flag_exists_in_b9360():
    flags = _census()
    missing = [
        (key, flag) for key, flag in llm_backend.FLAG_FOR.items()
        if flag not in flags
    ]
    assert missing == [], (
        "FLAG_FOR maps cfg keys to flags the installed build does not have: "
        f"{missing} — the Model screen would offer dead controls"
    )


def test_router_mode_flags_present():
    flags = _census()
    for needed in ("models-preset", "models-dir", "models-max", "models-autoload"):
        assert needed in flags, f"router-mode flag --{needed} missing from the fixture"


def test_census_detector_can_fail():
    """NEGATIVE CONTROL — a help text without a flag must not report it."""
    tiny = llm_backend.parse_supported_flags("--ctx-size N  set context")
    assert "ctx-size" in tiny
    assert "models-preset" not in tiny


def test_installed_flags_handles_missing_exe(monkeypatch):
    """No engine installed → empty census, no exception, no subprocess."""
    monkeypatch.setattr(llm_backend, "LLAMA_EXE", Path("Z:/nope/llama-server.exe"))
    llm_backend.installed_flags.cache_clear()
    try:
        assert llm_backend.installed_flags() == frozenset()
    finally:
        llm_backend.installed_flags.cache_clear()
