"""Selected methods cannot be silently withdrawn by live owner or direct edits."""
import json
from uuid import uuid4

import pytest
from _owned_seat import owned_app

from litetui import harness
from litetui import prompt_commands as commands
from litetui import prompt_overrides as prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["", " \n\t", "# POLYMATHIC GAMMA\nONE\n# POLYMATHIC GAMMA\nTWO", "\ufeff# POLYMATHIC GAMMA\nONE\n\ufeff# POLYMATHIC GAMMA\nTWO"])
async def test_real_owner_child_route_refuses_blank_or_duplicate_selected_method(tmp_path, monkeypatch, replacement):
    method = tmp_path / "selected.md"
    method.write_text("# POLYMATHIC GAMMA\nORIGINAL SELECTED METHOD", encoding="utf-8")
    parent = owned_app(spawn_identity=(str(uuid4()), "owner", "leader"))
    child = owned_app(cognitive_file=str(method))
    parent._system = child._system = lambda _: None
    parent.seat.registered = child.seat.registered = True
    harness.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for app in (parent, child):
        (harness.AGENTS_DIR / f"{app.seat.agent_id}.json").write_text(json.dumps({
            "agent_id": app.seat.agent_id, "name": app.seat.name,
            "spawned_by": parent.seat.agent_id if app is child else None}), encoding="utf-8")
    forwarded = []

    async def attach(identity, command):
        assert identity == child.seat.agent_id
        forwarded.append(command)
        return await commands.dispatch(child, command)

    monkeypatch.setattr(commands, "attach", attach)
    notified = []
    parent.seat.send = lambda *args: notified.append(args) or True
    folder = child._agent_session.memory_root / "prompts"
    before = (folder / "method.md").read_bytes()
    command = {"type": commands.REQUEST, "id": str(uuid4()), "action": "set", "seat": child.seat.agent_id,
               "convo": None, "name": "method.md", "reason": "method update", "text": replacement,
               "global_sha": prompts.sha("")}
    with pytest.raises(ValueError, match="blank|method|scaffold"):
        await commands.dispatch(parent, command)
    assert len(forwarded) == 1
    assert (folder / "method.md").read_bytes() == before
    assert not (folder / "audit.jsonl").exists()
    assert not notified


@pytest.mark.parametrize("replacement", ["", " \n\t"])
def test_selected_method_direct_file_edit_holds_actual_composition(tmp_path, replacement):
    method = tmp_path / "selected.md"
    method.write_text("# POLYMATHIC GAMMA\nSELECTED METHOD", encoding="utf-8")
    app = owned_app(cognitive_file=str(method))
    (app._agent_session.memory_root / "prompts" / "method.md").write_text(replacement, encoding="utf-8")
    prompts.refresh(app)
    with pytest.raises(ValueError, match="cannot be blank"):
        app._request_messages()


def test_unselected_interactive_method_may_remain_empty():
    app = owned_app()
    assert not app._seat_method_selected
    text = app._request_messages()[0]["content"]
    assert "# BUILDER ROLE" in text and "# POLYMATHIC" not in text


@pytest.mark.asyncio
async def test_real_parent_updates_unmaterialized_child_method_then_actual_system(tmp_path, monkeypatch):
    parent = owned_app(spawn_identity=(str(uuid4()), "owner", "leader"))
    child = owned_app()
    parent._system = child._system = lambda _: None
    parent.seat.registered = child.seat.registered = True
    parent.seat.send = lambda *args: True  # transport only; disk/request below prove receipt
    harness.AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    for app in (parent, child):
        (harness.AGENTS_DIR / f"{app.seat.agent_id}.json").write_text(json.dumps({
            "agent_id": app.seat.agent_id, "name": app.seat.name,
            "spawned_by": parent.seat.agent_id if app is child else None}), encoding="utf-8")

    async def attach(identity, command):
        assert identity == child.seat.agent_id
        return await commands.dispatch(child, command)

    monkeypatch.setattr(commands, "attach", attach)
    assert child.store.pending and not child.store.owned
    replacement = "# POLYMATHIC DIJKSTRA\nNEW LIVE METHOD"
    command = {"type": commands.REQUEST, "id": str(uuid4()), "action": "set", "seat": child.seat.agent_id,
               "convo": None, "name": "method.md", "reason": "select child method", "text": replacement,
               "global_sha": prompts.sha("")}
    result = await commands.dispatch(parent, command)
    assert result["status"] == "APPLIED"
    assert (child._agent_session.memory_root / "prompts" / "method.md").read_text() == replacement
    prompts.refresh(child)
    system = child._request_messages()[0]["content"]
    assert system.count("# BUILDER ROLE") == 1 and system.count("# POLYMATHIC DIJKSTRA") == 1
    assert "NEW LIVE METHOD" in system and child._seat_method_selected
