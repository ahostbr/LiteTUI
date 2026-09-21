from datetime import datetime, timedelta
from types import SimpleNamespace
from litetui.loop_list import loop_countdown


def job(seconds=65, enabled=True):
    now = datetime(2026, 1, 1)
    return SimpleNamespace(enabled=enabled, next_run_at=(now + timedelta(seconds=seconds)).isoformat()), now


def test_countdown_and_paused():
    j, now = job()
    assert loop_countdown(j, now) == 'Next in 01:05'
    assert loop_countdown(j, now + timedelta(seconds=1)) == 'Next in 01:04'
    j.enabled = False
    assert loop_countdown(j, now) == 'Paused'


def test_due_missing_and_hours():
    j, now = job(-2)
    assert loop_countdown(j, now) == 'Due'
    j.next_run_at = None
    assert loop_countdown(j, now) == 'Not scheduled'
    j, now = job(3661)
    assert loop_countdown(j, now) == 'Next in 1:01:01'

import pytest
from textual.app import App
from litetui.loop_list import LoopListBody


@pytest.mark.asyncio
async def test_panel_updates_from_live_schedule(monkeypatch):
    import litetui.loop_list as module
    j, now = job()
    j.id, j.prompt, j.interval_minutes, j.run_count = 'abc', 'check', 10, 0
    j.next_run_at = (datetime.now() + timedelta(seconds=65)).isoformat()
    monkeypatch.setattr(module, '_loop_jobs', lambda app: [j])
    class Host(App):
        def compose(self): yield LoopListBody()
    async with Host().run_test() as pilot:
        body = pilot.app.query_one(LoopListBody)
        label = body.query_one('#ll-next-abc')
        assert 'Next in' in str(label.render())
        j.enabled = False
        body._refresh_countdowns()
        assert 'Paused' in str(label.render())
        j.enabled = True
        j.next_run_at = (datetime.now() - timedelta(seconds=5)).isoformat()
        body._refresh_countdowns()
        assert 'Due' in str(label.render())
