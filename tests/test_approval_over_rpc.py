"""T577 — tool approval reaches the HOST over --rpc instead of a keyboard.

the user, 2026-09-10: "i want the approvals to route threw frontier chat GUI". A
LiteTUI child spawned headless by LiteSuite at the `interactive` profile has no
keyboard, so a `CONFIRM` decision had nowhere to go.

🔴 THESE ARMS REPLACE A SOURCE-GREP, AND THE REPLACEMENT IS THE POINT.
`test_no_dialog_over_rpc.py` used to assert `"_rpc" not in
inspect.getsource(show_dialog)` — a MECHANISM standing in for a POLICY ("a
headless child must never have approval silently converted into DENY-and-stop").
T577 satisfies that policy by ROUTING rather than refusing, and the routing goes
at the awaiting caller, so the old grep would have stayed green while the thing
it protected moved. A guard that passes for a reason that no longer holds is
worse than one that fails: it tells the next reader the question is settled.

So the policy is asserted directly: the event goes out, no screen appears, the
keyboard path is untouched, and an unanswered request is never reported as a
human refusal.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import harness, textfmt, tool_approval, tool_policy  # noqa: E402
from _owned_seat import owned_app


@pytest.fixture(autouse=True)
def disposable_presence(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)


def _registered_parent(a, parent):
    (harness.AGENTS_DIR / f"{parent}.json").write_text(
        json.dumps({"agent_id": parent, "spawned_by": None}), encoding="utf-8")
    a.seat.registered = True
    (harness.AGENTS_DIR / f"{a.seat.agent_id}.json").write_text(
        json.dumps({"agent_id": a.seat.agent_id, "spawned_by": parent}), encoding="utf-8")


def make_app(rpc: bool = False):
    a = owned_app()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    a._rpc = rpc
    from approval_store_fixture_t0340 import bind_origin
    bind_origin(a, harness.AGENTS_DIR / a.seat.agent_id, actual_edit=True)
    a.emitted: list = []
    a._rpc_emit = a.emitted.append
    return a


DECISION = tool_policy.PolicyDecision(
    action=tool_policy.CONFIRM,
    profile="interactive",
    capabilities=frozenset({"workspace_write"}),
    reason="writes to the workspace",
)


def _kinds(app) -> list[str]:
    return [e.get("type") for e in app.emitted if isinstance(e, dict)]


# ── 1. the request goes out, and no screen appears ──────────────────


@pytest.mark.asyncio
async def test_a_confirm_over_rpc_emits_the_request_and_pushes_no_screen() -> None:
    """Asserted on the emitted events AND the screen stack — never on anyone's
    source text, which is what the arm this replaces got wrong."""
    a = make_app(rpc=True)
    async with a.run_test(size=(100, 30)) as pilot:
        depth = len(a.screen_stack)
        task = asyncio.ensure_future(
            tool_approval.approve_over_rpc(a, "shell", {"command": "ls"}, DECISION)
        )
        for _ in range(6):
            await pilot.pause()

        assert "tool_approval_requested" in _kinds(a), (
            f"no approval request reached the host; emitted: {_kinds(a)}"
        )
        req = next(e for e in a.emitted if e.get("type") == "tool_approval_requested")
        assert req["tool"] == "shell"
        assert req["input"] == {"command": "ls"}
        assert req["why"] == "writes to the workspace"
        assert req["timeout_s"] == tool_approval.APPROVAL_TIMEOUT_S, (
            "the budget is not on the wire, so the host cannot show a countdown "
            "and has to guess when its answer stops being wanted"
        )
        assert len(a.screen_stack) == depth, "a screen was pushed over rpc"

        assert tool_approval.resolve_over_rpc(a, req["id"], allow=True)
        assert await task is tool_approval.ONCE
        task = None


@pytest.mark.asyncio
async def test_deny_and_remember_survive_the_wire() -> None:
    """The tri-state is the safety here (see ToolApproval.__bool__), so it has
    to arrive intact rather than collapsing to a bool at the boundary."""
    for allow, remember, want in (
        (False, False, tool_approval.DENIED),
        (True, False, tool_approval.ONCE),
        (True, True, tool_approval.ALWAYS),
    ):
        a = make_app(rpc=True)
        async with a.run_test(size=(100, 30)) as pilot:
            task = asyncio.ensure_future(
                tool_approval.approve_over_rpc(a, "shell", {}, DECISION)
            )
            for _ in range(6):
                await pilot.pause()
            req = next(e for e in a.emitted if e.get("type") == "tool_approval_requested")
            assert tool_approval.resolve_over_rpc(a, req["id"], allow, remember)
            assert await task == want, f"allow={allow} remember={remember}"


@pytest.mark.asyncio
async def test_an_unknown_id_is_refused_not_swallowed() -> None:
    """A silent success here means a host believing it allowed a call that had
    already been denied by timeout — the same doctrine `answer` carries."""
    a = make_app(rpc=True)
    async with a.run_test(size=(100, 30)):
        assert tool_approval.resolve_over_rpc(a, "appr-nope", allow=True) is False


@pytest.mark.asyncio
async def test_execute_tool_CHOOSES_the_wire_when_rpc_is_set(monkeypatch) -> None:
    """🔴 THE ROUTING, NOT THE WIRE — and they are different claims.

    Every arm above calls `approve_over_rpc` directly, so they prove the wire
    logic and say nothing about whether `_execute_tool` ever reaches it. T558-A
    made exactly this distinction for `ask_user_question` and its reproduction
    was the routing going the other way, so the same arm is owed here: the
    branch is the thing that can be wired wrong.
    """
    a = make_app(rpc=True)
    monkeypatch.setattr(tool_policy, "evaluate", lambda *args, **kw: DECISION)

    async with a.run_test(size=(100, 30)) as pilot:
        depth = len(a.screen_stack)
        task = asyncio.ensure_future(a._execute_tool("bash", {"command": "echo hi"}))
        for _ in range(300):
            if any(e.get("type") == "tool_approval_requested" for e in a.emitted):
                break
            await pilot.pause(0.01)

        assert "tool_approval_requested" in _kinds(a), (
            f"a headless app still took the dialog path; emitted: {_kinds(a)}"
        )
        assert len(a.screen_stack) == depth, "a screen was pushed over rpc"

        req = next(e for e in a.emitted if e.get("type") == "tool_approval_requested")
        tool_approval.resolve_over_rpc(a, req["id"], allow=False)
        text, ok = await task

    assert ok is False
    assert "denied by user" in text.lower(), (
        "a real Deny on the wire must read as the human refusal it is"
    )
    assert a._stop_requested, "DENY still stops the turn (Owner, 2026-08-24)"


@pytest.mark.asyncio
async def test_an_unanswered_approval_stops_the_turn_saying_so(monkeypatch) -> None:
    """The whole point, end to end: the refusal text AND the stop reason must
    both say nobody answered, not that a person refused."""
    a = make_app(rpc=True)
    monkeypatch.setattr(tool_policy, "evaluate", lambda *args, **kw: DECISION)
    monkeypatch.setattr(tool_approval, "APPROVAL_TIMEOUT_S", 0.05)
    # `bash` is used because it RESOLVES: `_execute_tool` looks the tool up
    # before the policy gate, so an invented name returns "unknown tool"
    # and never reaches the branch under test.

    async with a.run_test(size=(100, 30)):
        text, ok = await a._execute_tool("bash", {"command": "echo hi"})

    assert ok is False
    assert "no answer" in text.lower(), text
    assert "denied by user" not in text.lower(), (
        "an unanswered approval was reported to the model as a human refusal"
    )
    assert "no host answered" in (a._stop_reason or "").lower(), a._stop_reason


# ── 2. the negative control: a keyboard still gets the dialog ───────


@pytest.mark.asyncio
async def test_without_rpc_the_dialog_still_appears() -> None:
    """Without this, arm 1 is satisfied by breaking approval everywhere — a
    child that never asks anybody passes 'no screen appeared' perfectly."""
    a = make_app(rpc=False)
    async with a.run_test(size=(100, 30)) as pilot:
        from functools import partial

        from litetui.side_panel import show_dialog
        from litetui.tool_approval import ToolApprovalBody, ToolApprovalScreen

        a.settings.dialog_style = "modal"
        a.run_worker(
            show_dialog(
                a,
                partial(ToolApprovalBody, "shell", {"command": "ls"}, DECISION),
                modal_factory=partial(
                    ToolApprovalScreen, "shell", {"command": "ls"}, DECISION
                ),
            ),
            group="approval",
        )
        for _ in range(8):
            await pilot.pause()

        assert isinstance(a.screen, ToolApprovalScreen), (
            f"the keyboard path lost its dialog; screen={type(a.screen).__name__}"
        )
        assert _kinds(a) == [], "a non-rpc app put an approval on the wire"


# ── 3. no answer is never reported as a human refusal ───────────────


@pytest.mark.asyncio
async def test_a_timeout_returns_None_and_not_DENIED() -> None:
    """🔴 THE ARM THE OLD ONE EXISTED TO BECOME. `DENIED` and `None` both refuse
    the call, and merging them is the silent policy change: "the user refused"
    tells the model a person decided and it should stop asking, so a host that
    is simply broken would look like a settled decision forever."""
    a = make_app(rpc=True)
    async with a.run_test(size=(100, 30)):
        answer = await tool_approval.approve_over_rpc(
            a, "shell", {}, DECISION, timeout=0.05
        )

    assert answer is None, f"a timeout produced {answer!r} instead of None"
    assert answer is not tool_approval.DENIED


def test_the_transcript_says_the_host_never_answered() -> None:
    """The refusal TEXT is the part the model reads, so it is what is asserted —
    a return value the caller renders as "you denied" would fail this while
    passing every arm above."""
    text = textfmt.tool_denied("no-host", name="shell")

    assert "shell" in text
    lower = text.lower()
    assert "never answered" in lower or "did not respond" in lower, text
    assert "nobody decided" in lower, (
        "the refusal does not say a human was not involved"
    )
    assert "denied by user" not in lower, "an unanswered approval reads as a refusal"


def test_no_host_and_by_user_are_different_refusals() -> None:
    """A negative control on the pair: if someone later points `no-host` at the
    `by-user` section to save a file, every arm above still passes."""
    assert textfmt.tool_denied("no-host", name="x") != textfmt.tool_denied(
        "by-user", name="x"
    )


# T0210: native addressed mail can answer only the recorded spawner's RPC waiter.
@pytest.mark.asyncio
@pytest.mark.parametrize("verb,want", [("APPROVE", tool_approval.ONCE), ("DENY", tool_approval.DENIED)])
@pytest.mark.parametrize("payload", [False, True])
async def test_T0210_addressed_spawner_reply_resolves_rpc_once(monkeypatch, tmp_path, verb, want, payload):

    a = make_app(rpc=True)
    a._spawner_id = "recorded-leader"
    _registered_parent(a, "recorded-leader")
    delivered = []
    a._deliver_inbox = delivered.append
    new, done = tmp_path / "new", tmp_path / "done"
    new.mkdir()
    monkeypatch.setattr(harness, "NEW", new)
    monkeypatch.setattr(harness, "DONE", done)
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    task = asyncio.create_task(tool_approval.approve_over_rpc(a, "shell", {}, DECISION, timeout=0.2))
    await asyncio.sleep(0)
    ident = a.emitted[-1]["id"]
    msg = {"to": a.seat.agent_id, "from": "recorded-leader"}
    msg["payload" if payload else "body"] = {"text": f"{verb} {ident}\n"} if payload else f"{verb} {ident}\n"
    (new / "answer.json").write_text(json.dumps(msg), encoding="utf-8")
    for incoming in a.seat.poll():
        a._receive_mail(incoming)
    assert await task is want
    assert delivered == []
    assert a.seat.poll() == []
    a._receive_mail(msg)
    assert delivered == [msg], "a duplicate/late answer must not resolve a future twice"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sender", "id", "addressee", "broadcast", "missing-to", "no-spawner", "expired"])
async def test_T0210_wrong_or_expired_rpc_reply_never_grants_permission(kind):
    a = make_app(rpc=True)
    a._spawner_id = None if kind == "no-spawner" else "recorded-leader"
    _registered_parent(a, a._spawner_id)
    delivered = []
    a._deliver_inbox = delivered.append
    task = asyncio.create_task(tool_approval.approve_over_rpc(a, "shell", {}, DECISION, timeout=0.02))
    await asyncio.sleep(0)
    ident = a.emitted[-1]["id"]
    msg = {"to": a.seat.agent_id, "from": "recorded-leader", "body": f"APPROVE {ident}"}
    if kind == "sender":
        msg["from"] = "another-leader"
    elif kind == "id":
        msg["body"] = "APPROVE appr-000000000000"
    elif kind == "addressee":
        msg["to"] = "another-seat"
    elif kind == "broadcast":
        msg["to"] = "broadcast"
    elif kind == "missing-to":
        msg.pop("to")
    elif kind == "expired":
        msg.update(timestamp="2000-01-01T00:00:00+00:00", ttl_minutes=1)
    a._receive_mail(msg)
    assert await task is None
    assert delivered == [msg]
    assert not tool_approval.resolve_over_rpc(a, ident, True)


@pytest.mark.asyncio
async def test_T0210_registry_edit_does_not_change_pending_rpc_authority(monkeypatch, tmp_path):

    a = make_app(rpc=True)
    a._spawner_id = "recorded-leader"
    _registered_parent(a, "recorded-leader")
    a.seat.spawned_by = "recorded-leader"
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)
    task = asyncio.create_task(tool_approval.approve_over_rpc(a, "shell", {}, DECISION, timeout=0.2))
    await asyncio.sleep(0)
    ident = a.emitted[-1]["id"]
    (tmp_path / f"{a.seat.agent_id}.json").write_text(json.dumps({
        "agent_id": a.seat.agent_id, "spawned_by": "replacement-leader"}), encoding="utf-8")
    a.seat.spawned_by = "replacement-leader"  # presence changes future requests, not this pending one
    delivered = []
    a._deliver_inbox = delivered.append
    wrong = {"to": a.seat.agent_id, "from": "replacement-leader", "body": f"APPROVE {ident}"}
    a._receive_mail(wrong)
    assert delivered == [wrong] and not task.done()
    a._receive_mail({"to": a.seat.agent_id, "from": "recorded-leader", "body": f"DENY {ident}"})
    assert await task is tool_approval.DENIED
    assert a._spawner_id == "recorded-leader"
