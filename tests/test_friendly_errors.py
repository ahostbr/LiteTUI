"""Diagnostic copy remains actionable without losing the original detail."""

from litetui.friendly_errors import display_error
from litetui.settings import load, save


def test_litesuite_refusal_is_explained_without_making_chat_seem_broken():
    raw = ("[!] mcp litesuite-tools: MCPError: cannot reach "
           "http://localhost:7423/mcp: [WinError 10061] connection refused")
    assert display_error(raw) == (
        "LiteSuite tools are offline. Start LiteSuite to use them; chat and other tools still work."
    )
    assert display_error(raw, "detail") == raw


def test_other_mcp_server_and_unknown_error():
    assert display_error("[!] mcp docs: cannot reach http://localhost:9001") == (
        "docs tools are offline. Start their server, then use /mcp reconnect docs; chat still works."
    )
    assert display_error("[!] unusual failure: keep this detail") == "[!] unusual failure: keep this detail"


def test_mode_defaults_and_persists(tmp_path):
    settings = load(tmp_path)
    assert settings.error_message_style == "plain"
    settings.error_message_style = "detail"
    save(settings, tmp_path)
    assert load(tmp_path).error_message_style == "detail"
