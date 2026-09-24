"""Native Claude settings distinguish live authority from runtime-owned knobs."""
from types import SimpleNamespace

import pytest
from textual.app import App

from litetui.codex_settings import control, loop_description
from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen


def test_claude_authority_is_live_but_inventory_refresh_is_explicit():
    backend = SimpleNamespace(name="claude", owns_native_turns=True)
    for name in ("tools_enabled", "tools_disabled"):
        capability = control(backend, name)
        assert capability.editable
        assert "/claude new" in capability.help
    assert "123" not in loop_description(backend, 123)


@pytest.mark.asyncio
async def test_claude_settings_preserve_unsupported_values_and_disable_controls():
    saved = Settings(backend="claude", temperature=0.85, tool_iterations=123,
                     thinking_level="medium", default_context_length=65536)

    class Host(App):
        model_id = "default"
        backend = SimpleNamespace(name="claude", owns_native_turns=True,
                                  reasoning_levels=lambda key: ["low", "medium", "high", "xhigh", "max"])

        def on_mount(self):
            self.push_screen(SettingsScreen(saved))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        for name in ("temperature", "tool_iterations",
                     "default_context_length", "autocompact_enabled",
                     "mcp_enabled", "skills_enabled"):
            assert body.query_one(f"#f-{name}").disabled, name
        # Effort is Claude's own control now (Ryan 2026-09-24: "Yes, add effort levels too").
        for name in ("tools_enabled", "tools_disabled", "tool_policy_profile", "thinking_level"):
            assert not body.query_one(f"#f-{name}").disabled, name
        collected = body._collect()
        assert collected.temperature == 0.85
        assert collected.tool_iterations == 123
        assert collected.thinking_level == "medium"
        assert collected.default_context_length == 65536


def test_claude_recovery_command_is_discoverable_and_routes(monkeypatch):
    from litetui import claude_turn
    from litetui.app import LiteTUI

    app = LiteTUI()
    entry = app.plugins.commands["/claude"]
    assert "resolve" in entry.help and "continue" in entry.help
    called = []
    monkeypatch.setattr(claude_turn, "command", lambda host, arg: called.append((host, arg)))
    app._handle_command("/claude status")
    assert called == [(app, "status")]
