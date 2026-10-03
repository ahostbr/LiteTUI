"""Drive shipped native wire seams, substituting only provider transports."""
import json
from types import SimpleNamespace as NS

import pytest

from litetui import claude_turn, paths, prompt_overrides as prompts
from litetui.app import LiteTUI
from test_claude_turn import turn_app, result_event
from test_codex_app_server import Server
from litetui.codex_app_server import AppServerTransport


def write(directory, name, text):
    folder = directory / "prompts"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")


@pytest.mark.asyncio
async def test_real_claude_query_next_turn_reset_and_input_provenance(tmp_path, monkeypatch):
    app = turn_app(tmp_path, messages=["result"], events=[[result_event()]])
    app.settings.user_name = ""
    ledger = app._claude_ledger
    segment = ledger.selected
    ledger.fix_system_prompt(segment["id"], "immutable FLOOR mutable-policy version 1", host_policy_version=1)
    session = app.backend.session
    queried = []
    async def query(entry_id, content):
        queried.append(content)
    session.query = query
    write(tmp_path, "ordinary-instructions.md", "FIRST ORDINARY")
    write(tmp_path, "seat-instructions.md", "FIRST SEAT")
    snapshot = prompts.refresh(app)
    app._claude_active_input["_litetui_prompt_snapshot"] = snapshot
    write(tmp_path, "ordinary-instructions.md", "MIDTURN CHANGE")
    await claude_turn.stream_turn(app)
    assert len(queried) == 1 and "FIRST ORDINARY" in queried[-1] and "MIDTURN CHANGE" not in queried[-1]
    assert queried[-1].endswith("hello") and "not user-authored" in queried[-1]
    first = ledger.segment(segment["id"])["entries"][0]
    assert first["content"] == "hello"
    assert "FIRST ORDINARY" in first["litetui_ordinary_context"]
    write(tmp_path, "ordinary-instructions.md", "SECOND ORDINARY")
    write(tmp_path, "seat-instructions.md", "")
    app._events = [[result_event()]]
    item = claude_turn.prepare_input(app, "second user", "strict", "typed")
    app._claude_active_input = {"content": "second user", **item, "_litetui_prompt_snapshot": prompts.refresh(app)}
    await claude_turn.stream_turn(app)
    assert len(queried) == 2 and "SECOND ORDINARY" in queried[-1]
    assert "FIRST SEAT" not in queried[-1] and "FIRST ORDINARY" not in queried[-1]
    assert queried[-1].endswith("second user")
    # Reset via retained inactive metadata, no physical deletion.
    from litetui import prompt_commands
    from test_prompt_overrides_t0338 import command
    for name in ("ordinary-instructions.md", "seat-instructions.md"):
        prompt_commands.apply(tmp_path, command(tmp_path, name, action="reset", text=""))
    app._events = [[result_event()]]
    item = claude_turn.prepare_input(app, "third user", "strict", "typed")
    app._claude_active_input = {"content": "third user", **item, "_litetui_prompt_snapshot": prompts.refresh(app)}
    await claude_turn.stream_turn(app)
    assert len(queried) == 3 and "## Engineering practices" in queried[-1] and "SECOND ORDINARY" not in queried[-1]


@pytest.mark.asyncio
async def test_manually_placed_ordinary_on_quoted_marker_legacy_holds_query(tmp_path):
    app = turn_app(tmp_path, messages=["result"], events=[[result_event()]])
    ledger = app._claude_ledger
    segment = ledger.selected
    ledger.fix_system_prompt(segment["id"], 'old higher priority text quotes "mutable-policy version 1"')
    write(tmp_path, "ordinary-instructions.md", "MUST NOT SEND")
    app._claude_active_input["_litetui_prompt_snapshot"] = prompts.refresh(app)
    session = app.backend.session
    await claude_turn.stream_turn(app)
    assert session.queried == []
    assert ledger.pending(segment["id"])[0]["state"] == "prepared"
    assert any("/claude new" in note for note in app.notices)


def test_fresh_claude_native_system_has_only_floor_not_mutable():
    app = LiteTUI()
    app._materialise_convo()
    ledger = claude_turn.ledger_for(app)
    segment = ledger.select_segment(str(app.convo_dir))
    write(app.convo_dir, "ordinary-instructions.md", "MUTABLE ORDINARY")
    write(app.convo_dir, "seat-instructions.md", "MUTABLE SEAT")
    text = claude_turn.system_prompt_for(app, segment)
    assert "Shared-worktree caveat" in text
    assert "## Engineering practices" not in text and "MUTABLE ORDINARY" not in text and "MUTABLE SEAT" not in text
    assert "mutable-policy version 1" in text
    assert ledger.selected["host_policy_version"] == 1


@pytest.mark.asyncio
async def test_codex_actual_transport_next_turn_instructions(tmp_path, monkeypatch):
    from litetui import codex_history
    app = LiteTUI()
    app._materialise_convo()
    app.model_id = "model"
    app.backend = NS(name="codex", models={"model": {}}, app_server=True)
    server = Server()
    transport = AppServerTransport(server, app)
    monkeypatch.setattr(codex_history, "hydrate", lambda *a: None)
    # Real reconciliation has no history fetch for a new thread; suppress only
    # remote resumption on subsequent fixture turns, not instruction building.
    from litetui import codex_steering
    async def reconcile(*args):
        pass
    monkeypatch.setattr(codex_steering, "reconcile_messages", reconcile)
    write(app.convo_dir, "ordinary-instructions.md", "CODEX FIRST")
    prompts.refresh(app)
    app.conversation.append({"role": "user", "content": "first"})
    async def collect(messages):
        stream = await transport.create(model="model", messages=messages, stream=True,
                                        tools=[], max_tokens=32, extra_body={"reasoning_effort": "high"})
        async for _ in stream:
            pass
    await collect(app._request_messages())
    start = next(params for method, params in server.requests if method == "thread/start")
    assert "CODEX FIRST" in start["developerInstructions"] and "Shared-worktree caveat" in start["developerInstructions"]
    app.conversation.append({"role": "assistant", "content": "OK", "provider_metadata": {
        "provider": "codex", "app_server_thread_id": "thread-1", "instructions_digest": "old"}})
    app.conversation.append({"role": "user", "content": "second"})
    write(app.convo_dir, "ordinary-instructions.md", "CODEX SECOND")
    prompts.refresh(app)
    await collect(app._request_messages())
    turn = [params for method, params in server.requests if method == "turn/start"][-1]
    text = turn["additionalContext"]["litetui-instructions"]["value"]
    assert "CODEX SECOND" in text and "CODEX FIRST" not in text
    assert "Shared-worktree caveat" in text
