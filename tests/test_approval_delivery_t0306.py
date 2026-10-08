"""No-parent escalation is a waiting notice, never a rejected approval."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from approval_store_fixture_t0340 import bind_origin
from litetui import approval_authority as authority
from litetui import approval_delivery as delivery
from litetui import approval_relay, harness

IDENT = "appr-a9f2e196419c"
REQUESTER, APPROVER, PARENT = "worker", "original-approver", "parent"
TIMEOUT = 1.5


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(delivery, "ESCALATE_AFTER_S", 0.6)
    (tmp_path / f"{APPROVER}.json").write_text(json.dumps({"agent_id": APPROVER}))
    notices, sends = [], []
    noticed = asyncio.Event()

    def show(text):
        notices.append(text)
        noticed.set()

    app = SimpleNamespace(
        seat=SimpleNamespace(agent_id=REQUESTER,
                             send=lambda *args, **kwargs: sends.append(args) or True),
        conversation=[{"role": "user", "content": "sandbox approval"}],
        _edit=lambda *args: None, _system=show)
    bind_origin(app, tmp_path / "origin")
    return app, notices, sends, noticed


async def check_notice(pending, *, expected, outcome, authority_timeout=TIMEOUT,
                       delivery_timeout=TIMEOUT):
    app, notices, sends, noticed = pending
    future = asyncio.get_running_loop().create_future()
    app._relay_pending = {IDENT: (future, APPROVER)}
    frozen = authority.create(app, IDENT, approver=APPROVER, route="spawner", timeout=authority_timeout)
    # Pin only the display origin; all answer/authority clocks remain real.
    app._approval_authority_records[IDENT]["created_wall_time"] = 1700000000.0
    task = asyncio.create_task(delivery.wait_for_answer(
        app, future, approver=APPROVER, ident=IDENT, message="sandbox request",
        timeout=delivery_timeout, created_at=frozen.created_at))
    try:
        await asyncio.wait_for(noticed.wait(), 1.2)
        assert notices == [expected]
        assert not task.done() and not future.done()
        assert app._relay_answer_deadlines[IDENT] == frozen.created_at + delivery_timeout
        assert app._approval_authority_records[IDENT]["authority"] is frozen
        assert app._approval_authority_records[IDENT]["outcome"] == "pending"
        state = app.conversation[0]["approval_delivery"][IDENT]
        assert state["notice"] == expected
        assert state["state"] == ("escalation_failed" if frozen.ancestors else "escalation_absent")
        # Notification alone never lets a non-approver answer before eligibility.
        assert not approval_relay.take_answer(
            app, {"to": REQUESTER, "from": PARENT, "body": f"APPROVE {IDENT}"})
        assert not future.done()
        if outcome == "approve":
            assert approval_relay.take_answer(
                app, {"to": REQUESTER, "from": APPROVER, "body": f"APPROVE {IDENT}"})
            assert await task is True
        else:
            with pytest.raises(TimeoutError):
                await task
            assert not future.done()
            assert not approval_relay.take_answer(
                app, {"to": REQUESTER, "from": APPROVER, "body": f"APPROVE {IDENT}"})
        assert app._relay_answer_deadlines == {}
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["approve", "timeout"])
async def test_no_ancestor_notice_keeps_original_approval_and_deadline(pending, outcome):
    await check_notice(pending, outcome=outcome, expected=(
        f"Approval {IDENT}: nobody above approver {APPROVER} "
        "in this request's escalation chain. "
        f"Request still waiting for {APPROVER} until 2023-11-14T22:13:21.500+00:00."))
    assert pending[2] == [], "no ancestor must not attempt notification transport"


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_timeout,delivery_timeout,until", [
    (None, TIMEOUT, "until 2023-11-14T22:13:21.500+00:00"),
    (TIMEOUT, 3.0, "until 2023-11-14T22:13:21.500+00:00"),
    (3.0, TIMEOUT, "until 2023-11-14T22:13:21.500+00:00"),
    (None, float("inf"), "indefinitely (no deadline)"),
])
async def test_notice_displays_existing_effective_deadline(
        pending, authority_timeout, delivery_timeout, until):
    await check_notice(pending, outcome="approve", authority_timeout=authority_timeout,
                       delivery_timeout=delivery_timeout, expected=(
        f"Approval {IDENT}: nobody above approver {APPROVER} "
        "in this request's escalation chain. "
        f"Request still waiting for {APPROVER} {until}."))
    assert pending[2] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_real_delivery_failure_notice_is_unchanged(pending, tmp_path, raises):
    app, _, sends, _ = pending
    (tmp_path / f"{APPROVER}.json").write_text(json.dumps(
        {"agent_id": APPROVER, "spawned_by": PARENT}))
    (tmp_path / f"{PARENT}.json").write_text(json.dumps({"agent_id": PARENT}))

    def failed_send(target, body, **metadata):
        sends.append(target)
        if raises:
            raise OSError("sandbox transport failure")
        return False

    app.seat.send = failed_send
    await check_notice(pending, outcome="approve", expected=(
        f"Approval {IDENT}: escalation notification delivery failed or unconfirmed "
        "at deadline; request remains gated."))
    assert sends == [PARENT]
