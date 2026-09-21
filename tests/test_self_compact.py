import copy
import json
from types import SimpleNamespace as NS

import pytest

from litetui.app import WAKE_AFTER_COMPACT, LiteTUI
from litetui.plugins.self_compact import CompactionRequest
from litetui.settings import Settings


@pytest.fixture
def app():
    a = LiteTUI()
    a.settings = Settings(compact_keep_recent=2, autocompact_enabled=False,
                          wake_after_compact=False, clear_screen_after_compact=False)
    a.conversation = [
        {"role": "system", "content": "Test agent"},
        {"role": "user", "content": "Find the bug and then fix it"},
        {"role": "assistant", "content": "Investigating"},
        {"role": "user", "content": "Preserve the evidence"},
        {"role": "assistant", "content": "The cause is isolated"},
    ]
    a._system = lambda *args: None
    return a


ARGS = {"reason": "Investigation resolved", "handoff": "Cause: stale reference. Next: patch it; preserve load behavior."}


def test_request_is_deferred_validated_and_requires_progress(app):
    state = app.self_compaction
    before = copy.deepcopy(app.conversation)
    assert state.request({"reason": "", "handoff": "x"}).startswith("[error]")
    assert state.request({**ARGS, "handoff": "x" * 8001}).startswith("[error]")
    assert "not completed" in state.request(ARGS)
    assert state.request(ARGS).startswith("[error]")
    assert app.conversation == before
    assert state.take().handoff == ARGS["handoff"]
    assert state.request(ARGS).startswith("[error]")
    state.progress()
    assert "not completed" in state.request(ARGS)


def test_rejects_native_provider_nested_compaction_and_short_history(app, monkeypatch):
    state = app.self_compaction
    app.backend = NS(name="codex")
    assert state.request(ARGS).startswith("[error]")
    assert "self_compact" not in {s["function"]["name"] for s in app._all_tools()}
    app.backend = NS(name="ninfer")
    monkeypatch.setattr(state, "compacting", lambda: True)
    assert state.request(ARGS).startswith("[error]")
    monkeypatch.setattr(state, "compacting", lambda: False)
    app.conversation = app.conversation[:2]
    assert state.request(ARGS).startswith("[error]")


@pytest.mark.parametrize("changed", ["conversation", "busy", "stopped", "provider"])
def test_new_turn_or_conversation_cancels_scheduled_request(app, changed):
    request = CompactionRequest(**ARGS, conversation_id=app.convo_id)
    if changed == "conversation":
        app.convo_id = "different"
    elif changed == "busy":
        app._chat_running = lambda: True
    elif changed == "provider":
        app.backend = NS(name="codex")
    else:
        app._stop_requested = True
    calls = []
    app._compact = lambda *a, **kw: calls.append(kw)
    app._run_self_compaction(request)
    assert calls == []


class Stream:
    def __init__(self, content=None, calls=None):
        self.delta = NS(content=content, reasoning_content=None, tool_calls=calls)

    def __aiter__(self):
        async def chunks():
            yield NS(choices=[NS(delta=self.delta, finish_reason="stop")], usage=None)
        return chunks()

    async def close(self):
        pass


def call(index, name, args):
    return NS(index=index, id=f"call-{index}", function=NS(name=name, arguments=json.dumps(args)))


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "stop"])
async def test_real_round_compacts_after_all_results_and_resumes_with_handoff(app, tmp_path, monkeypatch, outcome):
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("confirmed evidence", encoding="utf-8")
    app.model_id = "fixture"
    app.available_models = ["fixture"]
    app._kick_card_summary = lambda *args: None
    async def ready(**kwargs):
        pass
    app._ensure_chat_ready = ready
    if outcome == "stop":
        execute = app._execute_tool
        async def stop_after_read(name, args):
            result = await execute(name, args)
            if name == "read":
                app._stop_requested = True
            return result
        app._execute_tool = stop_after_read
    requests = []
    snapshots = []
    records = []
    write_record = app.store.write_record
    def record(row):
        records.append(copy.deepcopy(row))
        write_record(row)
    monkeypatch.setattr(app.store, "write_record", record)
    async def create(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        snapshots.append(copy.deepcopy(app.conversation))
        if len(requests) == 1:
            return Stream(calls=[call(0, "self_compact", ARGS), call(1, "read", {"path": str(evidence)})])
        if len(requests) == 2:
            if outcome == "failure":
                raise RuntimeError("Test provider failure")
            return Stream("Investigation finished; preserve evidence and implement the fix.")
        if len(requests) == 3:
            return Stream(calls=[call(0, "self_compact", ARGS)])
        return Stream("Continuing with the fix.")
    monkeypatch.setattr("litetui.app.model_transport.for_app", lambda a: NS(create=create))
    events = []
    app._rpc_emit = events.append
    async with app.run_test() as pilot:
        app._stream()
        for _ in range(160):
            await pilot.pause(.05)
            if (any(e.get("stopReason") == "stop" for e in events)
                    or any(e.get("type") == "compaction" and e.get("reason") == "failed" for e in events)
                    or (outcome == "stop" and not app._chat_running())):
                break
    if outcome == "stop":
        assert len(requests) == 1
        assert app.self_compaction.pending is None
        assert [m["name"] for m in app.conversation if m["role"] == "tool"] == ["self_compact", "read"]
        assert not any(e.get("type") == "compaction" for e in events)
        return
    if outcome == "failure":
        assert len(requests) == 2
        assert app.conversation == snapshots[1]
        assert not any(m.get("content") == WAKE_AFTER_COMPACT for m in app.conversation)
        return
    assert len(requests) == 4
    # Both calls are paired before compaction starts, even when compact was first.
    results = [m for m in snapshots[1] if m["role"] == "tool"]
    assert [m["name"] for m in results] == ["self_compact", "read"]
    assert "confirmed evidence" in results[-1]["content"]
    assert "self_compact" not in {s["function"]["name"] for s in requests[1].get("tools", [])}
    assert ARGS["handoff"] in str(requests[2]["messages"])
    assert snapshots[2][-1]["content"] == WAKE_AFTER_COMPACT
    assert "Continue useful work" in str(snapshots[3])
    assert sum(e.get("type") == "compaction" and e.get("reason") == "compacted" for e in events) == 1
    saved = next(r for r in records if r["type"] == "truncate")
    assert saved["measurements"]["trigger"] == "agent"
    assert ARGS["handoff"] in str(saved)
