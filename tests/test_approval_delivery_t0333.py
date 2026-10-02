"""T0333 input scheduling/escalation: no tool execution or real mailbox."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from litetui import approval_delivery as delivery
from litetui import approval_relay, harness, hook_host
from litetui.app import LiteTUI
from litetui.settings import Settings

IDENT = "appr-a9f2e196419c"
REQUESTER, APPROVER, GRANDPARENT = "requesting-worker", "approver-id", "orchestrator-id"


def request_body(ident=IDENT):
    return (f"[APPROVAL {ident}] Worker (12345678) asks to run test_tool during a harness turn\n"
            "Danger: confirmation; why: test only\nInput: {}\n"
            f"[DELIVERY requester={REQUESTER} approver={APPROVER}]\n"
            f"Answer by inbox with exactly one line: APPROVE {ident}  or  DENY {ident}\n"
            "No answer within 600 s = the turn stops and this is logged.")


def request(ident=IDENT):
    return {"from": REQUESTER, "to": APPROVER, "body": request_body(ident),
            "type": "QUESTION", "thread_id": json.dumps({"kind": delivery.REQUEST_TYPE,
                "id": ident, "requester": REQUESTER, "approver": APPROVER})}


class BusyQueue(SimpleNamespace):
    def __init__(self):
        super().__init__(_pending_input=[], bubbles=[], settings=Settings(), _stop_requested=False)

    def _chat_running(self):
        return True

    def _user_bubble(self, text, has_image, queued=False):
        self.bubbles.append((text, queued))


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)


def test_busy_approval_reaches_next_safe_boundary_before_routine_backlog(monkeypatch):
    app = BusyQueue()
    for number in range(80):
        LiteTUI._deliver_inbox(app, {"from": "routine-sender", "body": f"progress {number}"})
    LiteTUI._deliver_inbox(app, request())
    admitted = []
    monkeypatch.setattr(hook_host, "accept_prompt", lambda app, item: admitted.append(item) or True)
    assert LiteTUI._deliver_queued_input(app)
    assert IDENT in admitted[0]["content"], "approval waited behind routine inbox backlog"
    assert [item["text"].splitlines()[-1] for item in app._pending_input] == [
        f"progress {number}" for number in range(80)]
    assert all(queued for _, queued in app.bubbles)
    assert not app._stop_requested


def test_priority_stable_and_does_not_overtake_native_or_owner_blockers():
    for blocker in ({"_codex_entry": {"state": "uncertain"}},
                    {"_claude_entry": {"id": "old-owner"}}, {"source": "interrupted"}):
        routine = {"text": "progress"}
        queue = [blocker, routine]
        delivery.enqueue(queue, {"text": "first"}, IDENT)
        delivery.enqueue(queue, {"text": "second"}, "appr-72cd808358ae")
        assert queue[0] is blocker
        assert [i["text"] for i in queue[1:]] == ["first", "second", "progress"]


@pytest.mark.parametrize("changes", [{"from": "stray"}, {"to": "stray"},
                                     {"type": "notification"}, {"thread_id": "bogus"},
                                     {"thread_id": "[]"}, {"thread_id": "{}"}])
def test_prose_or_mismatched_envelope_not_priority(changes):
    assert delivery.request_id({**request(), **changes}) is None


def test_notification_validates_exact_registered_lineage(tmp_path):
    (tmp_path / f"{APPROVER}.json").write_text(json.dumps(
        {"agent_id": APPROVER, "spawned_by": GRANDPARENT}), encoding="utf-8")
    assert delivery.request_id({**request(), "to": GRANDPARENT}) == IDENT
    (tmp_path / f"{APPROVER}.json").write_text("{}", encoding="utf-8")
    assert delivery.request_id({**request(), "to": GRANDPARENT}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("recipient,send_ok,expected", [(GRANDPARENT, True, "escalation_sent"),
    (GRANDPARENT, False, "escalation_failed"), (None, True, "escalation_absent")])
async def test_unanswered_escalates_once_without_changing_reply_authority(
        tmp_path, monkeypatch, recipient, send_ok, expected):
    monkeypatch.setattr(delivery, "ESCALATE_AFTER_S", 0.01)
    if recipient:
        (tmp_path / f"{APPROVER}.json").write_text(json.dumps(
            {"agent_id": APPROVER, "spawned_by": recipient}), encoding="utf-8")
    sends, said, stages = [], [], []
    monkeypatch.setattr(delivery, "stage", lambda ident, state: stages.append(state))
    future = asyncio.get_running_loop().create_future()
    def send(to, body, **metadata):
        assert metadata == {"approval_request": (IDENT, APPROVER)}
        sends.append((to, body))
        return send_ok
    saved = []
    app = SimpleNamespace(seat=SimpleNamespace(agent_id=REQUESTER, send=send),
                          conversation=[{"role": "user", "content": "test input"}],
                          _edit=lambda *args: saved.append(args),
                          _system=said.append, _relay_pending={IDENT: (future, APPROVER)})
    waiting = asyncio.create_task(delivery.wait_for_answer(app, future, approver=APPROVER,
        ident=IDENT, message=request_body(), timeout=0.15))
    for _ in range(100):
        if expected in stages:
            break
        await asyncio.sleep(0.002)
    assert expected in stages
    assert app.conversation[0]["approval_delivery"][IDENT]["state"] == expected
    assert saved == [(0, "Approval delivery state updated")]
    assert len(sends) == (1 if recipient else 0)
    if sends:
        assert sends[0][0] == GRANDPARENT and "Notification only" in sends[0][1]
    assert app._relay_pending[IDENT][1] == APPROVER
    assert not approval_relay.take_answer(app, {"from": GRANDPARENT, "body": f"APPROVE {IDENT}"})
    assert not future.done()
    assert approval_relay.take_answer(app, {"from": APPROVER, "body": f"DENY {IDENT}"})
    assert await waiting is False
    assert not approval_relay.take_answer(app, {"from": APPROVER, "body": f"APPROVE {IDENT}"})
    assert said


@pytest.mark.asyncio
async def test_answer_before_escalation_has_no_second_send(monkeypatch):
    monkeypatch.setattr(delivery, "ESCALATE_AFTER_S", 0.1)
    future = asyncio.get_running_loop().create_future()
    future.set_result(True)
    def unexpected_send(*args):
        raise AssertionError("answered request escalated")
    app = SimpleNamespace(seat=SimpleNamespace(agent_id=REQUESTER, send=unexpected_send))
    assert await delivery.wait_for_answer(app, future, approver=APPROVER,
        ident=IDENT, message=request_body(), timeout=0.2) is True


def test_plain_mail_cannot_gain_priority_by_full_approval_body():
    app = BusyQueue()
    LiteTUI._deliver_inbox(app, {"from": "routine", "body": "first"})
    spoof = {"from": REQUESTER, "to": APPROVER, "type": "QUESTION", "body": request_body()}
    LiteTUI._deliver_inbox(app, spoof)
    assert delivery.request_id(spoof) is None
    assert "first" in app._pending_input[0]["text"]
    assert "approval_request_id" not in app._pending_input[1]


@pytest.mark.parametrize("thread", ["x" * 1025, "null", "[]", "{}", 42])
def test_unbounded_or_unstructured_thread_not_priority(thread):
    assert delivery.request_id({**request(), "thread_id": thread}) is None


def test_structured_cli_roundtrip_uses_authoritative_writer_in_sandbox(tmp_path, monkeypatch):
    """Real installed CLI parsing/writer; HOME changed before its import."""
    import subprocess
    import sys
    from pathlib import Path

    interpreter = Path(sys.base_prefix) / ("python.exe" if sys.platform == "win32" else "bin/python3")
    runner = Path(__file__).with_name("t0333_cli_sandbox.py")
    home = tmp_path / "home"
    registry = home / ".liteharness" / "agents"
    registry.mkdir(parents=True)
    for aid in (REQUESTER, APPROVER, GRANDPARENT):
        (registry / f"{aid}.json").write_text(json.dumps({"agent_id": aid}), encoding="utf-8")
    monkeypatch.setattr(harness, "AGENTS_DIR", registry)
    monkeypatch.setattr(harness, "INBOX_ROOT", home / ".liteharness" / "inbox")
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    argvs = []
    def cli(argv, **kwargs):
        argvs.append(argv)
        return subprocess.run([str(interpreter), str(runner), str(home), *argv],
                              capture_output=True, text=True, timeout=10, check=False)
    monkeypatch.setattr(harness, "_cli", cli)
    seat = harness.Seat(agent_id=REQUESTER, name="Worker", model="test")
    seat.registered = True
    assert seat.send(APPROVER, request_body(), approval_request=(IDENT, APPROVER))
    files = list((home / ".liteharness" / "inbox" / "new").glob("*.json"))
    assert len(files) == 1
    mail = json.loads(files[0].read_text(encoding="utf-8"))
    assert delivery.request_id(mail) == IDENT
    assert mail["body"] == request_body()
    assert mail["type"] == "QUESTION"
    assert "--thread-id" in argvs[0] and "--body-file" in argvs[0]
    assert not list((home / ".liteharness" / "inbox" / "tmp").glob("*.json"))
    (registry / f"{APPROVER}.json").write_text(json.dumps(
        {"agent_id": APPROVER, "spawned_by": GRANDPARENT}), encoding="utf-8")
    assert seat.send(GRANDPARENT, "notification only", approval_request=(IDENT, APPROVER))
    mails = [json.loads(path.read_text(encoding="utf-8")) for path in
             (home / ".liteharness" / "inbox" / "new").glob("*.json")]
    escalated = next(mail for mail in mails if mail["to"] == GRANDPARENT)
    assert delivery.request_id(escalated) == IDENT
    assert escalated["thread_id"] == mail["thread_id"]
    assert escalated["priority"] == mail["priority"]
    monkeypatch.setattr(harness, "harness_disabled", lambda: True)
    assert not seat.send(APPROVER, "test", approval_request=(IDENT, APPROVER))
    assert len(argvs) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("hooks_enabled", [False, True])
async def test_approval_priority_preserves_held_owner_and_merged_before_pop_gate(
        tmp_path, monkeypatch, hooks_enabled):
    from test_claude_turn import app_for

    from litetui.claude_turn import hold_input, prepare_input

    app = app_for(tmp_path)
    app.settings = Settings()
    app.backend.owns_native_turns = True
    app._stop_requested = False
    original = prepare_input(app, "previous delivery", "strict", "harness")
    ledger = app._claude_ledger
    ledger.update_delivery(original["_claude_entry"]["id"], "submitted")
    ledger.update_delivery(original["_claude_entry"]["id"], "uncertain")
    hold_input(app, {"content": "held original owner", "source": "harness"}, "uncertain")
    held = app._pending_input[0]
    evidence = ledger.file.read_bytes()
    app._chat_running = lambda: True
    app._user_bubble = lambda *args, **kwargs: None
    LiteTUI._deliver_inbox(app, request())
    urgent = next(item for item in app._pending_input if item.get("approval_request_id") == IDENT)
    assert app._pending_input[0] is held, "priority overtook a held Claude owner"
    assert app._pending_input[1] is urgent

    app.backend.name = "codex"
    if hooks_enabled:
        monkeypatch.setattr(hook_host, "snapshot", lambda app: SimpleNamespace(hooks=[object()], error=None))
        assert await hook_host.queued_prompt(app) is False
    else:
        assert LiteTUI._deliver_queued_input(app) is False
    assert app._pending_input == [held, urgent]
    assert held["_claude_segment"] == ledger.selected["id"]
    assert held["_claude_conversation"] == app.convo_id
    assert ledger.file.read_bytes() == evidence
