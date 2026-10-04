"""T0183: a local click can settle only its own live spawner relay request."""
import asyncio
import re

import pytest

from litetui import app as app_mod, approval_relay, harness, runtime_log, tool_policy
from litetui.tool_policy import INTERACTIVE

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"


def seat(monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(harness, "AGENTS_DIR", tmp_path)
    (tmp_path / f"{SPAWNER}.json").write_text(json.dumps({"agent_id": SPAWNER}))
    app = app_mod.LiteTUI()
    app.settings.tool_policy_profile = INTERACTIVE
    app._active_tool_profile = INTERACTIVE
    app._spawned_seat = True
    app._owner_seat = False
    app._spawner_id = SPAWNER
    app.seat.registered = True
    (tmp_path / f"{app.seat.agent_id}.json").write_text(json.dumps({"agent_id": app.seat.agent_id, "spawned_by": SPAWNER}))
    sent = []
    app.seat.send = lambda to, body, **metadata: sent.append((to, body)) or True
    monkeypatch.setattr(runtime_log, "record", lambda *args, **kwargs: True)
    from approval_store_fixture_t0340 import bind_origin
    bind_origin(app, tmp_path / 'origin', actual_edit=True)
    return app, sent


async def waiting(app, sent, tmp_path):
    task = asyncio.create_task(app._authorize_action(
        "bash", {"command": "rm -rf ./build"}, tool_policy.SHELL_POLICY,
        workspace=tmp_path))
    for _ in range(100):
        if sent and app.query(".human-approval"):
            break
        await asyncio.sleep(.01)
    assert sent and app.query(".human-approval")
    for _ in range(100):
        if app.query(".human-approval-allow") and app.query(".human-approval-deny"):
            break
        await asyncio.sleep(.01)
    assert app.query(".human-approval-allow") and app.query(".human-approval-deny")
    control = app.query_one(approval_relay.HumanApproval)
    for _ in range(200):
        target = app.query_one(".human-approval-allow")
        if control.is_mounted and target.region.width:
            hit, _ = app.get_widget_at(target.region.x + 2, target.region.y + 1)
            if hit is target:
                break
        await asyncio.sleep(.01)
    assert control.is_mounted and target.region.width
    assert control.ident == re.search(r"appr-[0-9a-f]{12}", sent[-1][1]).group(0)
    return task, control


@pytest.mark.asyncio
@pytest.mark.parametrize("button, approved", [(".human-approval-allow", True),
                                             (".human-approval-deny", False)])
async def test_local_click_settles_exact_wait_and_disappears(monkeypatch, tmp_path, button, approved):
    app, sent = seat(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        task, control = await waiting(app, sent, tmp_path)
        target = app.query_one(button)
        hit, _ = app.get_widget_at(target.region.x + 2, target.region.y + 1)
        log = app.query_one("#chat-log")
        assert hit is target, f"hit={hit} button={target.region} control={control.region} log={log.region} scroll={log.scroll_y}/{log.max_scroll_y}"
        # Pin the test viewport after the real control is visible; the app's
        # follow loop otherwise moves it during Pilot's three mouse events.
        app.settings.autoscroll = False
        await pilot.pause(.2)
        assert await pilot.click(button, offset=(8, 2))
        result = await asyncio.wait_for(task, 3)
        assert (result is None) is approved
        assert not app.query(".human-approval")
        assert not approval_relay.take_human_answer(app, control.ident, True)


@pytest.mark.asyncio
async def test_wrong_and_late_id_cannot_override_and_agent_sender_still_checked(monkeypatch, tmp_path):
    app, sent = seat(monkeypatch, tmp_path)
    async with app.run_test() as pilot:
        task, control = await waiting(app, sent, tmp_path)
        assert not approval_relay.take_human_answer(app, "appr-000000000000", True)
        assert not approval_relay.take_answer(app, {"from": "other-agent", "body": f"APPROVE {control.ident}"})
        assert not task.done()
        app.settings.autoscroll = False
        assert await pilot.click(".human-approval-deny", offset=(8, 2))
        await asyncio.wait_for(task, 3)
        assert not approval_relay.take_human_answer(app, control.ident, True)
