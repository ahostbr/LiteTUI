import asyncio
import copy

import pytest

from litetui.codex_steering import RejectedRequest, SteeringLedger
from litetui.model_transport import ProviderError


@pytest.mark.asyncio
@pytest.mark.parametrize("reject", [False, True])
async def test_live_transport_steers_while_host_tool_waits_and_retains_rejected_input(
    reject, monkeypatch
):
    from types import SimpleNamespace as NS

    from test_codex_app_server import Server

    from litetui import hook_host
    from litetui.codex_app_server import AppServerTransport

    release = asyncio.Event()
    admissions, snapshots, launched = [], [], []
    messages = [{"role": "user", "content": "begin"}]
    tools = [{"type": "function", "function": {"name": "echo", "parameters": {"type": "object"}}}]
    app = NS(
        conversation=messages,
        convo_id="local",
        _pending_input=[],
        _stop_requested=False,
        tools_enabled=True,
        settings=NS(tool_policy_profile="scheduled"),
        backend=NS(models={}),
        plugins=NS(deferred_specs=list, tool_specs=lambda: tools),
        _rpc=True,
        _rpc_emit=lambda e: None,
        _append=messages.append,
        _edit=lambda *a: snapshots.append(copy.deepcopy(messages)),
        _active_tool_profile="scheduled",
        hook_config=object(),
        _system=lambda text: None,
        rejected_prompts=[],
        _stream=lambda: launched.append(True),
    )

    async def dispatch(app, event, data, **kwargs):
        admissions.append((event, data))
        return NS(allowed=True, reason="")

    async def drain(app):
        pass

    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    monkeypatch.setattr(hook_host, "drain_lifecycle", drain)

    async def execute(name, args):
        app._pending_input.append(
            {"content": "steer me", "source": "queued", "tool_profile": "scheduled"}
        )
        await asyncio.wait_for(release.wait(), 2)
        return "OK", True

    app._execute_tool = execute

    class WaitingServer(Server):
        async def request(self, method, params):
            if method == "turn/start":
                await self.events.put(
                    {
                        "id": 99,
                        "method": "item/tool/call",
                        "params": {
                            "tool": "litetui_echo",
                            "arguments": {},
                            "callId": "call",
                        },
                    }
                )
                return {"turn": {"id": "active"}}
            if method == "turn/steer":
                self.requests.append((method, params))
                assert (
                    snapshots[-1][0]["provider_metadata"]["steering"][0]["state"]
                    == "sending"
                )
                release.set()
                if reject:
                    raise RejectedRequest("turn ended", -32600)
                return {"turnId": "active"}
            return await super().request(method, params)

        async def send(self, message):
            await super().send(message)
            await self.events.put(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "active", "status": "completed"},
                    },
                }
            )

    server = WaitingServer()
    transport = AppServerTransport(server, app)
    stream = await transport.create(model="gpt-6-astra", messages=messages, tools=tools, stream=True)
    async for _ in stream:
        pass
    assert len(admissions) == 1
    entry = messages[0]["provider_metadata"]["steering"][0]
    if reject:
        assert entry["state"] == "next_turn"
        item = app._pending_input.pop(0)
        hook_host.start_prompt(app, item)
        assert launched == [True] and len(admissions) == 1
        assert messages[-1]["codex_delivery"]["state"] == "pending"
        fallback_server = Server()
        fallback = AppServerTransport(fallback_server, app)
        stream = await fallback.create(
            model="gpt-6-astra", messages=messages, stream=True
        )
        async for _ in stream:
            pass
        request = next(
            params
            for method, params in fallback_server.requests
            if method == "turn/start"
        )
        assert request["clientUserMessageId"] == entry["id"]
        assert messages[-1]["codex_delivery"]["state"] == "accepted"
        assert len(admissions) == 1
    else:
        assert entry["state"] == "accepted" and not app._pending_input
        assert messages[-1]["content"] == "steer me"
        assert messages[-1]["codex_delivery"]["state"] == "accepted"
        assert messages[-1]["provider_metadata"]["app_server_thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_acceptance_persists_before_return_and_admission_runs_once():
    entries, saved, admissions, requests = [], [], [], []
    ledger = SteeringLedger(entries, lambda: saved.append(copy.deepcopy(entries)))
    entry = ledger.enqueue(
        {
            "content": [
                {"type": "text", "text": "queued"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,eA=="},
                },
            ],
            "bubble": object(),
        },
        "thread",
        "turn",
    )

    async def admit(item):
        admissions.append(item)
        assert saved[-1][0]["state"] == "admitting"
        return True, {"source": "queued"}

    async def request(method, params):
        assert saved[-1][0]["state"] == "sending"
        requests.append((method, params))
        return {"turnId": "turn"}

    assert await ledger.deliver(entry, admit=admit, request=request) == "accepted"
    assert await ledger.deliver(entry, admit=admit, request=request) == "accepted"
    assert len(admissions) == len(requests) == 1
    assert requests[0][1]["expectedTurnId"] == "turn"
    assert requests[0][1]["clientUserMessageId"] == entry["id"]
    assert [b["type"] for b in requests[0][1]["input"]] == ["text", "image"]
    assert "bubble" not in saved[0][0]["item"]
    assert saved[-1][0]["state"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,state",
    [
        (RejectedRequest("ended", -32600), "next_turn"),
        (RejectedRequest("internal", -32603), "uncertain"),
        (ProviderError("lost connection"), "uncertain"),
        (TimeoutError(), "uncertain"),
    ],
)
async def test_rejection_and_uncertainty_are_distinct_without_resending(error, state):
    entries, calls = [], []
    ledger = SteeringLedger(entries, lambda: None)
    entry = ledger.enqueue({"content": "queued"}, "thread", "turn")

    async def admit(item):
        return True, {}

    async def request(*args):
        calls.append(args)
        raise error

    assert await ledger.deliver(entry, admit=admit, request=request) == state
    restored = SteeringLedger(copy.deepcopy(entries), lambda: None)
    assert (
        await restored.deliver(restored.entries[0], admit=admit, request=request)
        == state
    )
    assert len(calls) == 1
    history = {"thread": {"id": "thread", "turns": [{"id": "turn", "items": []}]}}
    assert not restored.reconcile(restored.entries[0], history)
    history["thread"]["turns"][0]["items"].append(
        {"type": "userMessage", "clientId": entry["id"]}
    )
    assert restored.reconcile(restored.entries[0], history)
    assert restored.entries[0]["state"] == "accepted"


@pytest.mark.asyncio
async def test_denied_or_interrupted_admission_never_sends():
    entries = []
    ledger = SteeringLedger(entries, lambda: None)
    entry = ledger.enqueue({"content": "queued"}, "thread", "turn")

    async def admit(item):
        return False, {"reason": "refused"}

    async def request(*args):
        pytest.fail("Denied input must never reach Codex")

    assert await ledger.deliver(entry, admit=admit, request=request) == "denied"
    entry["state"] = "admitting"
    assert await ledger.deliver(entry, admit=admit, request=request) == "admitting"


@pytest.mark.asyncio
async def test_cancelled_send_preserves_identity_for_reconciliation():
    entries, calls = [], []
    ledger = SteeringLedger(entries, lambda: None)
    entry = ledger.enqueue({"content": "queued"}, "thread", "turn")

    async def admit(item):
        return True, {}

    async def request(*args):
        calls.append(args)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await ledger.deliver(entry, admit=admit, request=request)
    assert entry["state"] == "sending"
    restored = SteeringLedger(copy.deepcopy(entries), lambda: None)
    assert (
        await restored.deliver(restored.entries[0], admit=admit, request=request)
        == "sending"
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_resume_restores_uncertain_queue_then_materializes_only_with_native_proof():
    from types import SimpleNamespace as NS

    from litetui.codex_steering import recover_queue_head, restore_queue

    entry = {
        "id": "client",
        "threadId": "thread",
        "turnId": "turn",
        "state": "sending",
        "item": {"content": "queued"},
    }
    messages = [
        {
            "role": "user",
            "content": "original",
            "provider_metadata": {"steering": [entry]},
        }
    ]
    app = NS(
        conversation=messages,
        _pending_input=[],
        _edit=lambda *a: None,
        _append=messages.append,
        settings=NS(tool_policy_profile="scheduled"),
    )
    restore_queue(app)
    restore_queue(app)
    assert len(app._pending_input) == 1
    confirmed = False

    async def start():
        pass

    async def request(*args):
        return {
            "thread": {
                "id": "thread",
                "turns": [
                    {
                        "id": "turn",
                        "items": [{"type": "userMessage", "clientId": "client"}]
                        if confirmed
                        else [],
                    }
                ],
            }
        }

    server = NS(start=start, request=request)
    assert not await recover_queue_head(app, server)
    assert len(messages) == 1 and len(app._pending_input) == 1
    confirmed = True
    assert await recover_queue_head(app, server)
    restore_queue(app)
    assert len(messages) == 2 and not app._pending_input
    assert messages[-1]["content"] == "queued"
    assert messages[-1]["codex_delivery"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_uncertain_fallback_is_not_resent_without_matching_thread_identity():
    from types import SimpleNamespace as NS

    from litetui.codex_steering import reconcile_messages

    message = {
        "role": "user",
        "content": "queued",
        "codex_delivery": {"id": "client", "state": "sending", "threadId": "wanted"},
    }
    actual = "other"

    async def request(method, params):
        assert method == "thread/read"
        return {
            "thread": {
                "id": actual,
                "turns": [{"items": [{"type": "userMessage", "clientId": "client"}]}],
            }
        }

    server = NS(request=request)
    with pytest.raises(ProviderError, match="different thread"):
        await reconcile_messages(None, server, [message])
    assert message["codex_delivery"]["state"] == "sending"
    actual = "wanted"
    await reconcile_messages(None, server, [message])
    assert message["codex_delivery"]["state"] == "accepted"
    assert message["provider_metadata"]["app_server_thread_id"] == "wanted"


@pytest.mark.asyncio
async def test_failed_journal_write_keeps_queue_and_does_not_send():
    from types import SimpleNamespace as NS

    from litetui.codex_steering import HostSteering, save_at

    app = NS(
        _pending_input=[{"content": "queued"}],
        _stop_requested=False,
        _edit=lambda *a: None,
        store=NS(persist_error="disk failure", convo_path="existing", loading=False),
    )

    async def request(*args):
        pytest.fail("A delivery must be saved before it is sent")

    steering = HostSteering(
        app, NS(request=request), "thread", "turn", {}, lambda: save_at(app, 0)
    )
    with pytest.raises(ProviderError, match="could not be saved"):
        await steering.run()
    assert app._pending_input[0]["content"] == "queued"


@pytest.mark.asyncio
async def test_uncertain_delivery_survives_actual_conversation_store_reload(tmp_path):
    from litetui.conversation import ConversationRepository

    store = ConversationRepository()
    store.convo_dir = tmp_path
    store.convo_path = tmp_path / "conversation.jsonl"
    message = {
        "role": "user",
        "content": "original",
        "provider_metadata": {"steering": []},
    }
    try:
        store.record_msg(message)
        ledger = SteeringLedger(
            message["provider_metadata"]["steering"],
            lambda: store.record_edit(0, message),
        )
        entry = ledger.enqueue({"content": "queued"}, "thread", "turn")

        async def admit(item):
            return True, {"source": "queued"}

        async def request(*args):
            raise ProviderError("connection lost")

        assert await ledger.deliver(entry, admit=admit, request=request) == "uncertain"
        assert store.persist_error is None
        _, reloaded = ConversationRepository.read(store.convo_path)
        restored = reloaded[0]["provider_metadata"]["steering"][0]
        assert restored["id"] == entry["id"]
        assert restored["state"] == "uncertain"
        assert restored["item"]["content"] == "queued"
        assert restored["admission"]["source"] == "queued"
    finally:
        store.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["queue", "message"])
@pytest.mark.parametrize("broken", [False, True])
async def test_delivery_recovery_honors_paginated_history(route, broken):
    from types import SimpleNamespace as NS

    from litetui.codex_steering import (
        reconcile_messages,
        recover_queue_head,
        restore_queue,
    )

    entry = {"id": "client", "threadId": "thread", "turnId": "turn", "state": "sending",
             "item": {"content": "explicit answer"}}
    messages = [{"role": "user", "content": "original", "provider_metadata": {"steering": [entry]}}]
    app = NS(conversation=messages, _pending_input=[], _edit=lambda *args: None,
             _append=messages.append, settings=NS(tool_policy_profile="scheduled"))
    restore_queue(app)
    materialized = {"role": "user", "content": "explicit answer",
                    "codex_delivery": {"id": "client", "threadId": "thread", "state": "sending"}}
    calls = []

    async def start():
        pass

    async def request(method, params):
        calls.append((method, params))
        assert params["threadId"] == "thread"
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {"thread": {"id": "thread", "historyMode": "paginated", "turns": []}}
        assert method == "thread/turns/list"  # Recovery must never resend input.
        assert params["itemsView"] == "full"
        if "cursor" not in params:
            return {"data": [{"id": "older", "items": []}], "nextCursor": "second"}
        assert params["cursor"] == "second"
        if broken:
            return {"data": None}
        return {"data": [{"id": "turn", "items": [{"type": "userMessage", "clientId": "client"}]}]}

    server = NS(start=start, request=request)
    if route == "queue":
        assert await recover_queue_head(app, server) is (not broken)
        assert len(app._pending_input) == (1 if broken else 0)
        assert len(messages) == (1 if broken else 2)
    elif broken:
        with pytest.raises(ProviderError, match="invalid history page"):
            await reconcile_messages(None, server, [materialized])
        assert materialized["codex_delivery"]["state"] == "sending"
    else:
        await reconcile_messages(None, server, [materialized])
        assert materialized["codex_delivery"]["state"] == "accepted"
    assert len(calls) == 3
