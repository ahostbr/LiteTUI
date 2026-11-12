"""The title bar reaches a native frontend through model_state, never inside its hello window.

Ryan's Rust run (2026-11-12) stopped with "Management protocol fault: ActivationUncertain":
an event arrived between the frontend's gui.hello and its reply. The header re-emission
(c8f0e7f3) sent model_state on the first header refresh after ready, before hello completed.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_title_changes_reach_the_frontend_only_after_hello_and_only_when_changed(make_app):
    app = make_app()
    async with app.run_test():
        sent = []
        app._rpc = True
        app._rpc_emit = lambda event: sent.append(event)
        app._rpc_ready_sent = True
        app._rpc_header_sent = type(app)._header_text(app)
        app._gui_rpc_enabled = False
        # Before hello completes: nothing, whatever the header does.
        app.update_header()
        app.sub_title = "changed before hello"
        app.update_header()
        assert [e for e in sent if e.get("type") == "model_state"] == []
        # After hello: an unchanged header sends nothing; a changed one sends model_state once.
        app._gui_rpc_enabled = True
        app._rpc_header_sent = type(app)._header_text(app)
        app.update_header()
        assert [e for e in sent if e.get("type") == "model_state"] == []
        app.title = "LiteTUI renamed"
        app.update_header()
        app.update_header()
        states = [e for e in sent if e.get("type") == "model_state"]
        assert len(states) == 1 and states[0]["title"].startswith("LiteTUI renamed")
