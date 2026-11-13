"""Python's chat-log system lines reach a native frontend only when it asks, then as events.

The Rust frontend drew none of them ("Hooks disabled by LITETUI_HOOKS=off", "harness seat
online · …", "Unknown: /x — try /help"). Its client faults on any event before its startup
reads finish, so nothing goes out unasked: gui.system_lines returns the held lines, and only
after that reply has been written does each later line become a system_message event.
"""
from __future__ import annotations

import pytest

from litetui import gui_rpc, native_system_lines


@pytest.mark.asyncio
async def test_lines_are_held_until_asked_then_sent_as_events(make_app):
    app = make_app()
    async with app.run_test():
        sent = []
        app._rpc = True
        app._rpc_emit = lambda event: sent.append(event)
        app.__dict__.pop("_gui_system_lines_held", None)
        app._splash_mounted = False
        app.system_message("Hooks disabled by LITETUI_HOOKS=off")
        app._splash_mounted = True
        app.system_message("harness seat online · LiteTUI · 0123abcd")
        # Nothing unasked.
        assert [e for e in sent if e.get("type") == "system_message"] == []
        assert "system_lines" in gui_rpc.OPERATIONS
        reply = await gui_rpc.async_dispatch(app, {"type": "gui.system_lines", "version": 1, "id": 7})
        assert reply == {"version": 1, "lines": [
            {"text": "Hooks disabled by LITETUI_HOOKS=off", "before_splash": True},
            {"text": "harness seat online · LiteTUI · 0123abcd", "before_splash": False}]}
        # A line between the reply's computation and its write waits for the write.
        app.system_message("between")
        assert [e for e in sent if e.get("type") == "system_message"] == []
        native_system_lines.after_read(app)
        app.system_message("Unknown: /nope — try /help")
        lines = [e for e in sent if e.get("type") == "system_message"]
        assert [(e["text"], e["version"], e["before_splash"]) for e in lines] == [
            ("between", 1, False), ("Unknown: /nope — try /help", 1, False)]
        # Asking twice is refused rather than replaying.
        with pytest.raises(ValueError, match="already"):
            native_system_lines.read(app, {"version": 1})


@pytest.mark.asyncio
async def test_a_frontend_that_never_asks_gets_no_new_event(make_app):
    app = make_app()
    async with app.run_test():
        sent = []
        app._rpc = True
        app._rpc_emit = lambda event: sent.append(event)
        for n in range(native_system_lines.MAX_HELD + 10):
            app.system_message(f"line {n}")
        assert [e for e in sent if e.get("type") == "system_message"] == []
        assert len(app._gui_system_lines_held) <= native_system_lines.MAX_HELD
