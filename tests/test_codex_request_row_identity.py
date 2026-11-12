"""Regression from d8554ffa (T0338 reapply, 2026-10-18): Codex mid-turn steering never started.

`LiteTUI._request_messages` strips the host's private `litetui_prompt_*` bookkeeping from the
wire. It did that by copying EVERY row, but `codex_app_server` finds the accepted user row with
`original is m for m in messages`. With every row copied nothing matched, so `record_index`
stayed None, `HostSteering` was never created, and the "Codex accepted user turn" metadata edit
never happened: queued input could not steer the running Codex turn.
"""
from __future__ import annotations

import pytest
from test_codex_app_server import Server

from litetui import app as app_mod
from litetui import codex_steering, settings
from litetui.agent_launch_context import ordinary
from litetui.codex_app_server import AppServerTransport


@pytest.fixture
def app(tmp_path):
    session = ordinary(tmp_path, settings.load())
    made = app_mod.LiteTUI(agent_session=session)
    made._connect = lambda: None
    made._fetch_ctx_window = lambda: None
    made._apply_context_length = lambda: None
    made._materialise_convo()
    yield made
    try:
        made.store.release()
    finally:
        made._agent_session.release()


def test_request_rows_keep_identity_and_drop_only_private_keys(app):
    assert app.conversation[0].get("litetui_prompt_owner") == 1
    app._append({"role": "user", "content": "hello"})
    wire = app._request_messages()
    assert wire[-1] is app.conversation[-1], "rows without host bookkeeping must not be copied"
    assert not any(key.startswith("litetui_") for key in wire[0])
    assert app.conversation[0]["litetui_prompt_owner"] == 1, "the saved row keeps its provenance"


@pytest.mark.asyncio
async def test_codex_turn_finds_the_accepted_user_row_and_starts_steering(app, monkeypatch):
    app._append({"role": "user", "content": "hello"})
    started = []

    class Steering:
        def __init__(self, *args):
            started.append(args)

        async def run(self):
            return None

    monkeypatch.setattr(codex_steering, "HostSteering", Steering)
    monkeypatch.setattr(app, "run_worker", lambda work, **_kwargs: work.close(), raising=False)
    # The transport reads the Codex model catalogue; this app was built on the default backend.
    monkeypatch.setattr(app.backend, "models", {}, raising=False)
    transport = AppServerTransport(Server(), app)
    stream = await transport.create(model="gpt-6-astra", messages=app._request_messages(), stream=True)
    async for _ in stream:
        pass
    assert started, "the Codex turn never found its accepted user row"
    assert app.conversation[-1]["provider_metadata"]["app_server_thread_id"] == "thread-1"
