"""Real owner sessions and private auth dispatcher; no live registry or sockets."""
import json
import os
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from litetui import harness, local_rpc, paths, prompt_commands as commands, prompt_overrides as prompts
from litetui.agent_ownership import AgentSession
from litetui.agent_store import AgentStore
from litetui.claude_persistence import ClaudeLedger


@pytest.fixture
def owners(tmp_path):
    store = AgentStore(tmp_path)
    sessions = [AgentSession.create_fresh(store, name=name, agent_id=str(uuid4()),
                                          backend="claude", model="model", thinking_level="high")
                for name in ("Leader", "Worker")]
    apps = []
    for session in sessions:
        authority = session.authority
        cid = str(uuid4())
        directory = session.conversation_directory(cid)
        directory.mkdir(parents=True)
        (directory / "convo.jsonl").write_text('{}\n')
        notices = []
        app = NS(_agent_session=session, seat=NS(agent_id=authority.agent_id, name=authority.name,
                                               registered=True, send=lambda *args: True),
                 convo_id=cid, convo_dir=directory, _system=notices.append, notices=notices,
                 backend=NS(name="lmstudio"))
        app.store = NS(convo_id=cid, convo_dir=directory, owned=True, pending=False,
                       loading=False, agent_session=session)
        apps.append(app)
    harness.AGENTS_DIR.mkdir()
    leader, worker = apps
    for app in apps:
        (harness.AGENTS_DIR / f'{app.seat.agent_id}.json').write_text(json.dumps({
            "agent_id": app.seat.agent_id, "name": app.seat.name,
            "spawned_by": leader.seat.agent_id if app is worker else None}))
    yield apps
    for session in sessions:
        session.release()


def request(worker, **changes):
    return {"type": commands.REQUEST, "id": str(uuid4()), "action": "set", "seat": worker.seat.agent_id,
            "convo": worker.convo_id, "name": "ordinary-instructions.md", "reason": "improve work",
            "text": "new ordinary text", "global_sha": prompts.sha(prompts.read_text(paths.prompt_file("ordinary-instructions.md"))),
            **changes}


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [True, False])
async def test_owned_actor_target_apply_notification_dedup(owners, monkeypatch, sent):
    leader, worker = owners
    outgoing = []
    leader.seat.send = lambda to, body: outgoing.append((to, body)) or sent
    forwarded = []
    async def attached(identity, cmd):
        assert identity == worker.seat.agent_id
        forwarded.append(cmd)
        return await commands.dispatch(worker, cmd)
    monkeypatch.setattr(commands, "attach", attached)
    cmd = request(worker)
    result = await commands.dispatch(leader, cmd)
    assert result["status"] == ("APPLIED" if sent else "APPLIED_NOTIFICATION_FAILED")
    assert result["actor"] == leader.seat.agent_id
    assert outgoing[0][0] == worker.seat.agent_id and len(worker.notices) == 1
    assert forwarded[0]["actor"] == leader.seat.agent_id
    assert len(forwarded) == 2
    assert prompts.read_text(paths.prompt_file(cmd["name"], convo_dir=worker._agent_session.memory_root)) == cmd["text"]
    again = await commands.dispatch(leader, cmd)
    assert again["duplicate"] and len(outgoing) == 1 and len(worker.notices) == 1
    rows = [json.loads(s) for s in (worker._agent_session.memory_root / "prompts" / "audit.jsonl").read_text().splitlines()]
    assert [r["phase"] for r in rows] == ["intent", "applied", "notification"]
    assert rows[0]["who"] == leader.seat.agent_id


@pytest.mark.asyncio
async def test_apply_receipt_lost_never_implies_notification(owners, monkeypatch):
    leader, worker = owners
    sent = []
    leader.seat.send = lambda *args: sent.append(args) or True
    async def attached(identity, cmd):
        result = await commands.dispatch(worker, cmd)
        raise TimeoutError("response lost")
    monkeypatch.setattr(commands, "attach", attached)
    cmd = request(worker)
    with pytest.raises(TimeoutError):
        await commands.dispatch(leader, cmd)
    assert not sent
    direct = {**cmd, "type": commands.APPLY, "actor": leader.seat.agent_id, "notification_sent": None}
    result = await commands.dispatch(worker, direct)
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN"
    assert result["notification_sent"] is None


@pytest.mark.asyncio
async def test_notification_ack_lost_is_partial(owners, monkeypatch):
    leader, worker = owners
    async def attached(identity, cmd):
        if cmd["notification_sent"] is not None:
            raise TimeoutError("ack unavailable")
        return await commands.dispatch(worker, cmd)
    monkeypatch.setattr(commands, "attach", attached)
    result = await commands.dispatch(leader, request(worker))
    assert result["status"] == "APPLIED_NOTIFICATION_ACK_UNKNOWN"
    assert result["notification_sent"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong-convo", "unowned", "parent", "unknown-field", "cross-seat"])
async def test_refusal_before_publication(owners, fault):
    leader, worker = owners
    cmd = {**request(worker), "type": commands.APPLY, "actor": leader.seat.agent_id, "notification_sent": None}
    if fault == "wrong-convo":
        cmd["convo"] = str(uuid4())
    elif fault == "unowned":
        worker.store.owned = False
    elif fault == "parent":
        cmd["actor"] = str(uuid4())
    elif fault == "unknown-field":
        cmd["path"] = "arbitrary"
    elif fault == "legacy":
        worker.backend.name = "claude"
        ledger = worker._claude_ledger = ClaudeLedger(worker.convo_dir)
        segment = ledger.select_segment(str(worker.convo_dir))
        ledger.fix_system_prompt(segment["id"], "old mixed system")
        ledger.bind_session(segment["id"], "native-old")
    else:
        cmd["seat"] = leader.seat.agent_id
    with pytest.raises(ValueError):
        await commands.dispatch(worker, cmd)
    assert not (worker._agent_session.memory_root / "prompts").exists()


@pytest.mark.asyncio
async def test_explicit_fresh_claude_transition(owners):
    leader, worker = owners
    worker.backend.name = "claude"
    ledger = worker._claude_ledger = ClaudeLedger(worker.convo_dir)
    segment = ledger.select_segment(str(worker.convo_dir))
    ledger.fix_system_prompt(segment["id"], "old mixed system")
    ledger.bind_session(segment["id"], "native-old")
    cmd = {**request(worker), "type": commands.APPLY, "actor": leader.seat.agent_id, "notification_sent": None}
    # Publication is home-scoped; unsafe native sessions are held at admission,
    # not disguised as a prose-immutability ban in the publication endpoint.
    result = await commands.dispatch(worker, cmd)
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN"
    fresh = ledger.select_segment(str(worker.convo_dir), new=True)
    assert fresh["id"] != segment["id"]
    result = await commands.dispatch(worker, cmd)
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN" and result["notification_sent"] is None
    assert ledger.selected["id"] == fresh["id"]


class Writer:
    def __init__(self):
        self.frames = []
    def get_extra_info(self, name):
        return ("127.0.0.1", 1234)
    def write(self, raw):
        self.frames.append(json.loads(raw))
    async def drain(self):
        pass
    def close(self):
        pass
    async def wait_closed(self):
        pass


def reader(*frames):
    import asyncio
    r = asyncio.StreamReader(limit=local_rpc.MAX_FRAME + 1)
    for f in frames:
        r.feed_data((json.dumps(f) + '\n').encode())
    r.feed_eof()
    return r


@pytest.mark.asyncio
async def test_private_auth_gate_actual_owner_dispatch(owners):
    leader, worker = owners
    rpc = local_rpc.LocalRpc(worker)
    rpc.endpoint = {"agent_id": worker.seat.agent_id, "pid": os.getpid(), "nonce": "fixture"}
    cmd = {**request(worker), "type": commands.APPLY, "actor": leader.seat.agent_id, "notification_sent": None}
    w = Writer()
    await rpc._client(reader({"agent_id": worker.seat.agent_id, "nonce": "fixture", "token": "wrong"}, cmd), w)
    assert w.frames[0]["ok"] is False and not (worker._agent_session.memory_root / "prompts").exists()
    w = Writer()
    await rpc._client(reader({"agent_id": worker.seat.agent_id, "nonce": "fixture", "token": rpc.secret}, cmd), w)
    assert w.frames[-1]["ok"] and w.frames[-1]["result"]["status"] == "APPLIED_NOTIFICATION_UNKNOWN"
    assert w.frames[-1]["result"]["notification_sent"] is None
    assert "prompts.apply" not in local_rpc.OPERATIONS  # not an open GUI operation
