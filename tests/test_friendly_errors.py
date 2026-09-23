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


def test_mcp_listing_and_declaration_keep_success_context_and_raw_detail():
    raw = "MCPError: cannot reach http://localhost:9001: connection refused"
    assert "docs tools" in display_error("Could not reconnect docs: " + raw)
    declared = "Declared 'docs' in .mcp.json, but it did not start: " + raw
    assert display_error(declared).startswith("Declared 'docs' in .mcp.json, but it did not start.")
    assert display_error(raw, surface="mcp").startswith("this server tools are offline")
    assert display_error(raw, "detail", surface="mcp") == raw

def test_settings_plugin_and_theme_surfaces_preserve_detail():
    cases = (
        ("Cannot save — OSError: access denied", "settings", "Settings could not be saved"),
        ("Runtime apply failed — engine: timed out", "settings", "Settings were saved"),
        ("failed at activate: ImportError: missing", "plugin", "A plugin could not start"),
        ("custom theme 'bad' skipped: invalid color", "theme", "custom theme 'bad' could not load"),
    )
    for raw, surface, expected in cases:
        assert display_error(raw, surface=surface).startswith(expected)
        assert display_error(raw, "detail", surface=surface) == raw
    assert display_error("unexpected API fault", surface="backend") == "unexpected API fault"


def test_tool_result_display_preserves_raw_for_model_and_full_detail():
    raw = "[error] invalid tool arguments: expected JSON object"
    assert "argument shape" in display_error(raw, surface="tool")
    assert display_error(raw, "detail", surface="tool") == raw
