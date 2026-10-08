"""Offline capability owner never constructs an app or refreshes metadata."""
import json
import sys

import pytest
from litetui import cli, model_capabilities as capabilities


def test_pinned_claude_owner_distinguishes_haiku_generations():
    assert "high" in capabilities.offline_capabilities("claude", "sonnet")["thinking"]["levels"]
    assert capabilities.offline_capabilities("claude", "haiku")["thinking"]["levels"] == ["default"]
    assert capabilities.offline_capabilities("claude", "claude-haiku-4-5-20251001")["thinking"]["levels"] == ["default"]
    assert capabilities.offline_capabilities("claude", "claude-haiku-5-5")["thinking"]["levels"] == [
        "default", "low", "medium", "high", "xhigh", "max"]
    with pytest.raises(ValueError, match="absent"):
        capabilities.offline_capabilities("claude", "unknown-model")


def test_existing_codex_cache_only(tmp_path):
    path = tmp_path / "models_cache.json"
    path.write_text(json.dumps({"models": [{"slug": "known", "supported_reasoning_levels": [{"effort": "none"}, {"effort": "high"}]}]}))
    before = path.read_bytes()
    result = capabilities.offline_capabilities("codex", "known", cache=path)
    assert result["thinking"]["levels"] == ["default", "off", "high"]
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="no refresh"):
        capabilities.offline_capabilities("codex", "unknown", cache=path)
    with pytest.raises(ValueError, match="no refresh"):
        capabilities.offline_capabilities("codex", "known", cache=tmp_path / "missing")


def test_cli_readonly_path_precedes_app_import(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["litetui", "--capabilities", "--backend", "claude", "--model", "sonnet"])
    # Guard the import door rather than merely relying on no app construction.
    import builtins
    original = builtins.__import__
    def imports(name, *args, **kwargs):
        if name == "litetui.app":
            pytest.fail("Read-only capability CLI must not import the app")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", imports)
    cli.main()
    assert "high" in json.loads(capsys.readouterr().out)["thinking"]["levels"]


def test_unknown_backend_offline_refuses():
    with pytest.raises(ValueError, match="unavailable"):
        capabilities.offline_capabilities("lmstudio", "unknown")
