"""Shipped native SYSTEM seams; provider transport only is substituted.

These are isolated captures, not running-seat/restart proof. The old mutable
user-prefix protocol is deliberately replaced by native system-segment updates.
"""
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from _owned_seat import owned_app
from test_codex_app_server import Server

from litetui import claude_turn
from litetui import prompt_overrides as prompts
from litetui.codex_app_server import AppServerTransport


def write(app, name, text):
    (app._agent_session.memory_root / "prompts" / name).write_text(text, encoding="utf-8")
    prompts.refresh(app)


@pytest.mark.asyncio
async def test_claude_fresh_segment_emits_edited_system_preserves_context_and_frozen_store(monkeypatch):
    app = owned_app(initial_backend="claude", initial_model="claude-sonnet")
    app._materialise_convo()
    app._system = lambda _: None
    app._seat_started = True
    app._chat_running = lambda: False
    root = app._agent_session.memory_root
    (root / "memory.md").write_text("FROZEN MEMORY", encoding="utf-8")
    ledger = claude_turn.ledger_for(app)
    segment = ledger.select_segment(claude_turn.launch_workspace(app))
    first = claude_turn.system_prompt_for(app, segment)
    assert "FROZEN MEMORY" in first
    old_segment = ledger.selected
    ledger.bind_session(segment["id"], "old-native-id")
    app.conversation += [{"role": "user", "content": "KEEP PRIOR QUESTION"},
                         {"role": "assistant", "content": "KEEP PRIOR ANSWER"}]
    (root / "memory.md").write_text("MUST NOT REFRESH MEMORY", encoding="utf-8")
    write(app, "role.md", "NEW SYSTEM ROLE BODY")
    entry = claude_turn.prepare_input(app, "NEXT INPUT", "strict", "typed")
    fresh = ledger.selected
    assert fresh["id"] != segment["id"]
    assert ledger.segment(segment["id"])["system_prompt"] == old_segment["system_prompt"]
    opened = []

    async def open_session(segment, model, *, system_prompt, **kwargs):
        opened.append((segment, system_prompt))
        return NS(effort=None)

    monkeypatch.setattr(app.backend, "open_session", open_session)
    await claude_turn.session_for(app, app.backend, fresh, None)
    system = Path(opened[0][1]["path"]).read_text(encoding="utf-8")
    assert "NEW SYSTEM ROLE BODY" in system
    assert "KEEP PRIOR QUESTION" in system and "KEEP PRIOR ANSWER" in system
    assert "FROZEN MEMORY" in system and "MUST NOT REFRESH MEMORY" not in system
    assert entry["_claude_segment"] == fresh["id"]
    assert ledger.pending(fresh["id"])[0]["content"] == "NEXT INPUT"
    assert "ordinary guidance" not in ledger.pending(fresh["id"])[0]["content"]


def test_claude_pending_delivery_never_moves_or_replays_on_system_edit():
    app = owned_app(initial_backend="claude", initial_model="claude-sonnet")
    app._materialise_convo()
    app._system = lambda _: None
    app._seat_started = True
    ledger = claude_turn.ledger_for(app)
    segment = ledger.select_segment(claude_turn.launch_workspace(app))
    claude_turn.system_prompt_for(app, segment)
    prepared = ledger.prepare(segment["id"], "HELD ORIGINAL", "strict", "typed")
    write(app, "role.md", "EDITED ROLE")
    with pytest.raises(claude_turn.UncertainDelivery, match="pending"):
        claude_turn.prepare_input(app, "NEW INPUT", "strict", "typed")
    assert ledger.selected["id"] == segment["id"]
    assert [e["id"] for e in ledger.pending(segment["id"])] == [prepared["id"]]
    item = {"content": "HELD ORIGINAL", "_claude_entry": prepared, "_claude_segment": segment["id"], "_claude_conversation": app.convo_id}
    assert claude_turn.queue_ready(app, item) is False
    with pytest.raises(claude_turn.UncertainDelivery, match="remains held"):
        claude_turn.accept_input(app, item)


def test_legacy_claude_edit_explicitly_held_not_user_prefix_fallback():
    app = owned_app(initial_backend="claude", initial_model="claude-sonnet")
    app._materialise_convo()
    app._system = lambda _: None
    app._seat_started = True
    ledger = claude_turn.ledger_for(app)
    segment = ledger.select_segment(claude_turn.launch_workspace(app))
    ledger.fix_system_prompt(segment["id"], "LEGACY IMMUTABLE SYSTEM")
    write(app, "role.md", "EDITED ROLE")
    with pytest.raises(claude_turn.UncertainDelivery, match="/claude new"):
        claude_turn.prepare_input(app, "NEW INPUT", "strict", "typed")
    assert not ledger.pending(segment["id"])


@pytest.mark.asyncio
async def test_codex_actual_transport_next_turn_system_instructions(monkeypatch):
    from litetui import codex_history, codex_steering
    app = owned_app()
    app._materialise_convo()
    app.model_id = "model"
    app.backend = NS(name="codex", models={"model": {}}, app_server=True)
    server = Server()
    transport = AppServerTransport(server, app)
    monkeypatch.setattr(codex_history, "hydrate", lambda *a: None)

    async def reconcile(*args):
        pass

    monkeypatch.setattr(codex_steering, "reconcile_messages", reconcile)
    write(app, "role.md", "CODEX FIRST SYSTEM")
    app.conversation.append({"role": "user", "content": "first"})

    async def collect():
        stream = await transport.create(model="model", messages=app._request_messages(), stream=True,
                                        tools=[], max_tokens=32, extra_body={"reasoning_effort": "high"})
        async for _ in stream:
            pass

    await collect()
    start = next(params for method, params in server.requests if method == "thread/start")
    assert "CODEX FIRST SYSTEM" in start["developerInstructions"]
    app.conversation.append({"role": "assistant", "content": "OK", "provider_metadata": {
        "provider": "codex", "app_server_thread_id": "thread-1", "instructions_digest": "old"}})
    app.conversation.append({"role": "user", "content": "second"})
    write(app, "role.md", "CODEX SECOND SYSTEM")
    await collect()
    turn = [params for method, params in server.requests if method == "turn/start"][-1]
    text = turn["additionalContext"]["litetui-instructions"]["value"]
    assert "CODEX SECOND SYSTEM" in text and "CODEX FIRST SYSTEM" not in text
