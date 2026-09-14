import pytest
from test_codex_history import history, host
from test_codex_tool_ui import Host

from litetui.codex_history import reconcile
from litetui.codex_tool_ui import CodexToolUI
from litetui.widgets import FoldBlock


@pytest.mark.asyncio
async def test_proposed_plan_keeps_item_identity_and_authoritative_completion():
    app = Host()
    async with app.run_test():
        ui = CodexToolUI(app, thread_id="thread", turn_id="turn")
        await ui.plan({"turnId": "turn", "plan": [{"step": "Checklist", "status": "pending"}]})
        await ui.item({"type": "plan", "id": "proposal", "text": ""})
        await ui.proposed_plan({"itemId": "proposal", "delta": "Draft "}, delta=True)
        await ui.proposed_plan({"itemId": "proposal", "delta": "text"}, delta=True)
        await ui.item({"type": "plan", "id": "proposal", "text": ""})
        assert ui.records["proposal"]["result"] == "Draft text"
        completed = {"type": "plan", "id": "proposal", "text": "Authoritative final plan"}
        await ui.item(completed, True)
        count = len(app.events)
        await ui.item(completed, True)
        await ui.proposed_plan({"itemId": "proposal", "delta": "late"}, delta=True)
        ui.finish()
        assert len(app.events) == count
        assert len(app.query(FoldBlock)) == 2
        assert ui.records["proposal"]["result"] == completed["text"]
        assert ui.records["proposal"]["state"] == "completed"
        assert ui.records["plan:turn"]["result"] == "pending: Checklist"
        assert app.events[-1] == {
            "eventVersion": 1, "provider": "codex", "threadId": "thread", "turnId": "turn",
            "type": "plan_update", "id": "proposal", "planType": "proposed",
            "text": completed["text"], "status": "completed",
        }
        assert not app.running and not ui.proposed_plan_text


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["completed", "interrupted", "failed", "inProgress"])
async def test_recovered_proposed_plan_is_ordered_and_idempotent(state):
    app = host()
    data = history({"id": "proposal", "type": "plan", "text": "Plan content"},
                   {"id": "answer", "type": "agentMessage", "text": "Answer"}, status=state)
    assert await reconcile(app, data) == 1
    trace = app.conversation[0]["provider_metadata"]["display_trace"]["items"]
    assert [record["id"] for record in trace] == ["proposal", "answer"]
    assert trace[0]["kind"] == "plan"
    assert trace[0]["name"] == "Codex proposed plan"
    assert trace[0]["state"] == ("running" if state == "inProgress" else state)
    assert await reconcile(app, data) == 0


@pytest.mark.asyncio
async def test_interrupted_proposal_is_sanitized_and_never_claims_completion():
    app = Host()
    app._rpc = True
    ui = CodexToolUI(app)
    await ui.proposed_plan({"itemId": "proposal", "delta": "\x1b[31mOPENAI_API_KEY="}, delta=True)
    await ui.proposed_plan({"itemId": "proposal", "delta": "synthetic-secret\x1b[0m"}, delta=True)
    ui.finish()
    record = ui.records["proposal"]
    assert record["state"] == "interrupted"
    assert app.events[-1]["status"] == "interrupted"
    assert "synthetic-secret" not in record["result"] and "\x1b" not in record["result"]
    assert not ui.proposed_plan_text


@pytest.mark.asyncio
async def test_transport_routes_plan_deltas_and_saves_completed_text(monkeypatch):
    from types import SimpleNamespace

    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServer, AppServerTransport, collect

    async def forbid_start(*args):
        pytest.fail("offline proposed-plan test attempted to start Codex")

    monkeypatch.setattr(AppServer, "start", forbid_start)

    class PlanServer(Server):
        async def request(self, method, params):
            if method == "turn/start":
                for notification, payload in [
                    ("item/started", {"item": {"type": "plan", "id": "proposal", "text": ""}}),
                    ("item/plan/delta", {"itemId": "proposal", "delta": "Draft"}),
                    ("item/completed", {"item": {"type": "plan", "id": "proposal", "text": "Final"}}),
                ]:
                    await self.events.put({"method": notification, "params": {
                        "threadId": "thread-1", "turnId": "1", **payload,
                    }})
            return await super().request(method, params)

    events = []
    app = SimpleNamespace(
        conversation=[{"role": "user", "content": "Synthetic"}],
        plugins=SimpleNamespace(deferred_specs=list), backend=SimpleNamespace(models={}),
        tools_enabled=True, _rpc=True, _rpc_emit=events.append, _edit=lambda *args: None,
    )
    transport = AppServerTransport(PlanServer(), app)
    stream = await transport.create(model="gpt-6-astra", messages=app.conversation, stream=True)
    result = await collect(stream)
    updates = [event for event in events if event["type"] == "plan_update"]
    assert [event["text"] for event in updates] == ["", "Draft", "Final"]
    trace = result.choices[0].message.provider_metadata["display_trace"]["items"]
    plan = next(record for record in trace if record["id"] == "proposal")
    assert plan["state"] == "completed" and plan["result"] == "Final"
