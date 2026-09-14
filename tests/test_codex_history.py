from copy import deepcopy
from types import SimpleNamespace

import pytest

from litetui.codex_history import reconcile


def host(*, trace=None, turn="turn", save=None):
    metadata = {"provider": "codex", "app_server_thread_id": "thread"}
    if turn:
        metadata["app_server_turn_id"] = turn
    if trace is not None:
        metadata["display_trace"] = {"version": 1, "items": trace}
    edits = []
    return SimpleNamespace(conversation=[{"role": "user", "content": "original",
                                         "provider_metadata": metadata}],
                           _edit=save or (lambda *args: edits.append(args)), edits=edits)


def history(*items, status="completed", thread="thread"):
    return {"id": thread, "turns": [{"id": "turn", "status": status, "items": list(items)}]}


@pytest.mark.asyncio
async def test_recovers_missing_completion_and_items_without_execution_or_fake_timing():
    app = host(trace=[{"id": "cmd", "turnId": "turn", "kind": "commandExecution",
                       "state": "interrupted", "result": "disconnected"}])
    data = history({"id": "cmd", "type": "commandExecution", "command": "echo",
                    "status": "completed", "exitCode": 0, "aggregatedOutput": "done"},
                   {"id": "answer", "type": "agentMessage", "phase": "final_answer",
                    "text": "Finished."})
    original = deepcopy(data)
    assert await reconcile(app, data) == 1
    trace = app.conversation[0]["provider_metadata"]["display_trace"]["items"]
    assert trace[0]["state"] == "completed" and "done" in trace[0]["result"]
    assert trace[0]["durationMs"] is None
    assert trace[1]["phase"] == "final_answer"
    assert await reconcile(app, data) == 0
    assert len(app.edits) == 1
    assert app.conversation[0]["content"] == "original" and data == original


@pytest.mark.asyncio
async def test_turn_identity_recovers_before_first_record_but_never_guesses_legacy():
    data = history({"id": "answer", "type": "agentMessage", "text": "Recovered"})
    assert await reconcile(host(), data) == 1
    assert await reconcile(host(turn=None), data) == 0
    assert await reconcile(host(), history(thread="other")) == 0


@pytest.mark.asyncio
async def test_partial_history_preserves_missing_records_and_does_not_finish_running():
    old = [{"id": "cmd", "turnId": "turn", "state": "running", "result": "partial"}]
    app = host(trace=old)
    assert await reconcile(app, history({"id": "cmd", "type": "commandExecution",
                                         "status": "inProgress"})) == 0
    assert await reconcile(app, {"id": "thread", "turns": []}) == 0
    assert app.conversation[0]["provider_metadata"]["display_trace"]["items"] == old


@pytest.mark.asyncio
async def test_save_failure_restores_metadata_and_propagates():
    def fail(*args):
        raise OSError("save unavailable")

    app = host(save=fail)
    before = deepcopy(app.conversation)
    with pytest.raises(OSError, match="save unavailable"):
        await reconcile(app, history({"id": "a", "type": "agentMessage", "text": "done"}))
    assert app.conversation == before


@pytest.mark.asyncio
async def test_missing_tool_is_inserted_before_saved_final_answer():
    app = host(trace=[{"id": "answer", "turnId": "turn", "kind": "agentMessage",
                       "state": "completed", "result": "done"}])
    await reconcile(app, history(
        {"id": "cmd", "type": "commandExecution", "status": "completed"},
        {"id": "answer", "type": "agentMessage", "text": "done"}))
    trace = app.conversation[0]["provider_metadata"]["display_trace"]["items"]
    assert [entry["id"] for entry in trace] == ["cmd", "answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("history_mode", ["legacy", "paginated"])
async def test_real_transport_resume_reconciles_without_resending_history(history_mode):
    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServerTransport

    class HistoryServer(Server):
        async def request(self, method, params):
            if method == "thread/resume":
                self.requests.append((method, params))
                thread = history({"id": "a", "type": "agentMessage", "text": "old"})
                thread["historyMode"] = history_mode
                if history_mode == "paginated":
                    thread["turns"] = []
                return {"thread": thread}
            if method == "thread/turns/list":
                self.requests.append((method, params))
                return {"data": history({"id": "a", "type": "agentMessage", "text": "old"})["turns"]}
            return await super().request(method, params)

    app = host()
    app.conversation.append({"role": "user", "content": "next"})
    app.backend = SimpleNamespace(models={})
    app.plugins = SimpleNamespace(deferred_specs=list)
    app.tools_enabled = True
    app._stop_requested = False
    app._rpc = True
    app._rpc_emit = lambda event: None
    server = HistoryServer()
    transport = AppServerTransport(server, app)
    stream = await transport.create(model="gpt-6-astra", messages=app.conversation, stream=True)
    iterator = stream.__aiter__()
    await anext(iterator)
    assert app.conversation[0]["provider_metadata"]["display_trace"]["items"][0]["result"] == "old"
    sent = next(params for method, params in server.requests if method == "turn/start")
    assert sent["input"] == [{"type": "text", "text": "next", "text_elements": []}]
    await stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [None, "switch", "append", "backend", "chat"])
async def test_refresh_reads_only_and_discards_stale_results(race):
    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServerTransport

    app = host()
    app.backend = object()
    app._chat_running = lambda: False
    original = app.conversation

    class ReadServer(Server):
        async def request(self, method, params):
            self.requests.append((method, params))
            assert method == "thread/read"
            assert params == {"threadId": "thread", "includeTurns": len(self.requests) == 2}
            if race == "switch":
                app.conversation = []
            elif race == "append":
                app.conversation.append({"role": "user", "content": "new"})
            elif race == "backend":
                app.backend = object()
            elif race == "chat":
                app._chat_running = lambda: True
            return {"thread": history({"id": "a", "type": "agentMessage", "text": "done"})}

    server = ReadServer()
    changed = await AppServerTransport(server, app).refresh_history()
    assert changed == (1 if race is None else 0)
    assert len(server.requests) == 2
    assert ("display_trace" in original[0]["provider_metadata"]) == (race is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["changed", "unchanged", "error", "switched"])
async def test_reopen_worker_redraws_only_current_changed_history(monkeypatch, outcome):
    from pathlib import Path

    from litetui import model_transport
    from litetui.app import LiteTUI

    app = host()
    app.backend = object()
    app._chat_running = lambda: False
    rendered, notes = [], []
    app._render_resumed = lambda path, **kwargs: rendered.append(path)
    app._system = notes.append

    async def refresh():
        if outcome == "error":
            raise model_transport.ProviderError("unavailable")
        if outcome == "switched":
            app.conversation = []
        return 0 if outcome == "unchanged" else 1

    monkeypatch.setattr(model_transport, "for_app", lambda value: SimpleNamespace(refresh_history=refresh))
    await LiteTUI._refresh_native_history.__wrapped__(app, Path("conversation.jsonl"))
    assert len(rendered) == (1 if outcome == "changed" else 0)
    assert len(notes) == (1 if outcome == "error" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["interrupted", "failed", "inProgress"])
async def test_partial_native_answer_is_recovered_without_claiming_completion(state):
    app = host()
    data = history({"id": "answer", "type": "agentMessage", "text": "Partial answer",
                    "phase": "commentary"}, status=state)
    assert await reconcile(app, data) == 1
    record = app.conversation[0]["provider_metadata"]["display_trace"]["items"][0]
    assert record["result"] == "Partial answer"
    assert record["state"] == ("running" if state == "inProgress" else state)
    assert record["phase"] == "commentary"
    assert await reconcile(app, data) == 0


@pytest.mark.asyncio
async def test_rendered_recovery_replaces_saved_cards_without_duplicates(monkeypatch):
    from _settle import settle_until
    from test_deny_stops_the_turn import _app

    from litetui.codex_app_server import AppServer
    from litetui.widgets import FoldBlock, ToolMessage

    async def forbid_start(*args):
        pytest.fail("offline render test attempted to start Codex")

    monkeypatch.setattr(AppServer, "start", forbid_start)
    app = _app(None)
    async with app.run_test(size=(80, 30)) as pilot:
        metadata = host(trace=[{"id": "cmd", "turnId": "turn", "kind": "commandExecution",
                                "name": "command", "state": "interrupted",
                                "result": "disconnected\n" * 60, "ok": False}]).conversation[0]["provider_metadata"]
        metadata["display_trace"]["items"].extend([
            {"id": "plan:turn", "turnId": "turn", "kind": "plan", "result": "First plan"},
            {"id": "plan:other", "turnId": "other", "kind": "plan", "result": "Second plan"},
        ])
        app.conversation = ([{"role": "user", "content": f"Earlier message {i}"} for i in range(15)]
                            + [{"role": "user", "content": "original", "provider_metadata": metadata}])
        app._render_resumed(app.convo_path)
        await pilot.pause()
        assert len(app.query(ToolMessage)) == 1
        next(iter(app.query(ToolMessage))).set_expanded(True)
        plans = [card for card in app.query(FoldBlock) if not isinstance(card, ToolMessage)]
        assert len(plans) == 2
        plans[0].set_expanded(True)
        assert plans[1].expanded is False
        await pilot.pause()
        log = app.query_one("#chat-log")
        log.scroll_to(y=5, animate=False, force=True)
        assert await settle_until(pilot, lambda: log.scroll_y == 5)
        position = log.scroll_y
        assert position > 0
        await reconcile(app, history({"id": "cmd", "type": "commandExecution",
                                      "command": "echo", "status": "completed",
                                      "aggregatedOutput": "recovered\n" * 60, "exitCode": 0}))
        app._render_resumed(app.convo_path, preserve_view=True)
        assert await settle_until(pilot, lambda: log.scroll_y == position
                                  and len(app.query(ToolMessage)) == 1
                                  and next(iter(app.query(ToolMessage)))._ok is True)
        cards = list(app.query(ToolMessage))
        assert len(cards) == 1 and cards[0]._ok is True
        assert cards[0].expanded is True
        plans = [card for card in app.query(FoldBlock) if not isinstance(card, ToolMessage)]
        assert [card.expanded for card in plans] == [True, False]
        assert log.scroll_y == position
        assert "recovered" in cards[0]._result
        app._render_resumed(app.convo_path)
        await pilot.pause()
        assert len(app.query(ToolMessage)) == 1
