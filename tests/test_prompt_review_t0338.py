"""Same-reviewer R1/R2/R3 regressions through owner and real transport seams."""
import asyncio
import json
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from litetui import prompt_commands as commands, prompt_overrides as prompts
from litetui.app import LiteTUI
from litetui.model_transport import OpenAITransport
from test_prompt_commands_t0338 import owners, request
from test_prompt_overrides_t0338 import command, layer


def evidence(directory):
    return {str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*") if p.is_file() and not p.name.endswith(".lock")}


@pytest.mark.parametrize("failure", ["applied-receipt", "before-intent"])
@pytest.mark.parametrize("next_name", ["compact.md", "card-summary.md"])
def test_cross_id_uncertainty_barrier_preserves_all_evidence(tmp_path, monkeypatch, failure, next_name):
    first = command(tmp_path, "compact.md", "A body")
    append = commands.append_audit
    def injected(directory, row):
        if row["phase"] == ("applied" if failure == "applied-receipt" else "intent"):
            raise OSError("injected publication boundary")
        append(directory, row)
    monkeypatch.setattr(commands, "append_audit", injected)
    with pytest.raises(OSError):
        commands.apply(tmp_path, first)
    before = evidence(tmp_path)
    monkeypatch.setattr(commands, "append_audit", append)
    second = command(tmp_path, next_name, "B body")
    assert second["id"] != first["id"]
    with pytest.raises(ValueError, match="uncertain|without audit intent"):
        commands.apply(tmp_path, second)
    assert evidence(tmp_path) == before
    assert not (tmp_path / "prompts" / "revisions" / f'{second["id"]}.json').exists()


def test_uncertain_other_request_does_not_replay_historical_duplicate_or_ack(tmp_path, monkeypatch):
    old = command(tmp_path, "compact.md", "old body")
    initial, _ = commands.apply(tmp_path, old)
    assert initial["status"] == "APPLIED_NOTIFICATION_UNKNOWN" and initial["notification_sent"] is None
    append = commands.append_audit
    def injected(directory, row):
        if row["phase"] == "applied":
            raise OSError("receipt failed")
        append(directory, row)
    monkeypatch.setattr(commands, "append_audit", injected)
    with pytest.raises(OSError):
        commands.apply(tmp_path, command(tmp_path, "compact.md", "new body"))
    monkeypatch.setattr(commands, "append_audit", append)
    before = evidence(tmp_path)
    duplicate, _ = commands.apply(tmp_path, old)
    assert duplicate["duplicate"] and duplicate["current_revision"] is False
    assert evidence(tmp_path) == before
    acknowledged, _ = commands.apply(tmp_path, {**old, "notification_sent": True})
    assert acknowledged["status"] == "APPLIED" and acknowledged["current_revision"] is False
    after = evidence(tmp_path)
    assert {p: b for p, b in after.items() if p != "prompts/audit.jsonl"} == {
        p: b for p, b in before.items() if p != "prompts/audit.jsonl"}
    assert after["prompts/compact.md"] == b"new body"
    with pytest.raises(ValueError, match="uncertain"):
        commands.apply(tmp_path, command(tmp_path, "card-summary.md", "still blocked"))


def test_completed_supersession_and_reset_remain_available(tmp_path):
    first = command(tmp_path, "compact.md", "first")
    commands.apply(tmp_path, first)
    second = command(tmp_path, "compact.md", "second")
    commands.apply(tmp_path, second)
    result, _ = commands.apply(tmp_path, first)
    assert result["current_revision"] is False
    assert (tmp_path / "prompts" / "compact.md").read_text() == "second"
    commands.apply(tmp_path, {**second, "id": str(uuid4()), "action": "reset", "text": ""})
    assert prompts.scoped_file(tmp_path, "compact.md") is None


@pytest.mark.asyncio
async def test_cancelled_leader_after_publication_remains_pending_no_automatic_resend(owners, monkeypatch):
    leader, worker = owners
    outgoing = []
    leader.seat.send = lambda *args: outgoing.append(args) or True
    async def attached(identity, cmd):
        await commands.dispatch(worker, cmd)
        raise asyncio.CancelledError()
    monkeypatch.setattr(commands, "attach", attached)
    cmd = request(worker)
    with pytest.raises(asyncio.CancelledError):
        await commands.dispatch(leader, cmd)
    assert not outgoing
    result = await commands.dispatch(worker, {**cmd, "type": commands.APPLY,
                                             "actor": leader.seat.agent_id, "notification_sent": None})
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN" and result["notification_sent"] is None
    assert result["duplicate"]


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_system", [True, False])
async def test_actual_request_to_openai_single_system_override_and_reset(existing_system):
    app = LiteTUI()
    app._materialise_convo()
    prefix = "legacy custom immutable floor stays verbatim"
    app.conversation = ([{"role": "system", "content": prefix}] if existing_system else []) + [
        {"role": "user", "content": "real user"}]
    app._store_injected = True
    app._inject_index_once = lambda: None
    stored = json.loads(json.dumps(app.conversation))
    captured = []
    async def create(**kwargs):
        messages = kwargs["messages"]
        # Model-template validator at the actual provider client boundary.
        assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
        assert isinstance(messages[0]["content"], str)
        captured.append(messages)
        return NS()
    transport = OpenAITransport(NS(chat=NS(completions=NS(create=create))))
    layer(app.convo_dir, "ordinary-instructions.md", "OVERRIDE")
    prompts.refresh(app)
    await transport.create(model="single-system-qwen", messages=app._request_messages())
    assert "OVERRIDE" in captured[-1][0]["content"]
    if existing_system:
        assert captured[-1][0]["content"].startswith(prefix + "\n\n")
    assert app.conversation == stored
    commands.apply(app.convo_dir, command(app.convo_dir, action="reset", text=""))
    prompts.refresh(app)
    await transport.create(model="single-system-qwen", messages=app._request_messages())
    assert "OVERRIDE" not in captured[-1][0]["content"]
    assert "## Engineering practices" in captured[-1][0]["content"]
    assert app.conversation == stored


@pytest.mark.parametrize("systems", [
    [{"role": "system", "content": []}],
    [{"role": "user", "content": "user"}, {"role": "system", "content": "late"}],
    [{"role": "system", "content": "first"}, {"role": "system", "content": "second"}],
])
def test_malformed_or_misplaced_system_refuses_before_wire(systems):
    app = LiteTUI()
    app._materialise_convo()
    app.conversation = systems
    app._store_injected = True
    app._inject_index_once = lambda: None
    before = json.loads(json.dumps(systems))
    layer(app.convo_dir, "ordinary-instructions.md", "override")
    prompts.refresh(app)
    with pytest.raises(ValueError, match="one initial string system"):
        app._request_messages()
    assert app.conversation == before
