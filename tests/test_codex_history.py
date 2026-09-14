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
async def test_real_transport_resume_reconciles_without_resending_history():
    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServerTransport

    class HistoryServer(Server):
        async def request(self, method, params):
            if method == "thread/resume":
                self.requests.append((method, params))
                return {"thread": history({"id": "a", "type": "agentMessage", "text": "old"})}
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
