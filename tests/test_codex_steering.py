import asyncio
import copy

import pytest

from litetui.codex_steering import RejectedRequest, SteeringLedger
from litetui.model_transport import ProviderError


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
