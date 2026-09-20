from types import SimpleNamespace
import pytest
from litetui import app as app_mod, scheduler
from litetui.widgets import UserMessage


@pytest.mark.parametrize('busy', [True, False])
def test_loop_header_never_enters_prompt(tmp_path, monkeypatch, busy):
    text = '  EXACT prompt\nwith second line  '
    job = scheduler.Job.loop(prompt=text, interval_minutes=5, owner_convo_id='owner',
                             now='2026-08-23T12:00:00')
    bubbles, submitted, pending = [], [], []
    app = SimpleNamespace(convo_id='owner', jobs=[job], _pending_input=pending,
        _chat_running=lambda: busy,
        _user_bubble=lambda text, image, **kwargs: bubbles.append((text, kwargs)))
    monkeypatch.setattr(app_mod.paths, 'ROOT', tmp_path)
    monkeypatch.setattr(app_mod.hook_host, 'start_prompt', lambda app, item: submitted.append(item))
    app_mod.LiteTUI._fire_job(app, job)
    assert bubbles[0][0] == job.prompt
    assert bubbles[0][1]['header'] == f'loop {job.id} · {job.schedule}'
    item = (pending if busy else submitted)[0]
    assert item['content'] == job.prompt
    if busy:
        assert item['text'] == job.prompt


def test_loop_header_survives_delivery_and_folding():
    widget = UserMessage('exact prompt', queued=True, header='loop 916039bd · @every 5m')
    for action in (lambda: None, widget.mark_delivered, lambda: widget.set_collapsed(True)):
        action()
        assert 'loop 916039bd · @every 5m' in str(widget.border_title)
        assert 'You' not in str(widget.border_title)
    assert widget.preview == 'exact prompt'
