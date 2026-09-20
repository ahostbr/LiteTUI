"""Explicit lock supersedes automatic position/anchor relocking (Ryan's ruling)."""
from types import SimpleNamespace
from litetui.app import LiteTUI


def test_unlocked_never_autorelocks_on_send():
    app = SimpleNamespace(settings=SimpleNamespace(autoscroll=False))
    # No viewport lookup or deferred scroll permitted, even for Send.
    LiteTUI._scroll_down(app, reader_acted=True)


def test_position_inference_removed():
    assert not hasattr(LiteTUI, '_still_following')
    assert not hasattr(LiteTUI, '_reader_left_follow_tail')


def test_explicit_lock_toggle_invalidates_queued_work():
    calls = []
    app = SimpleNamespace(
        settings=SimpleNamespace(autoscroll=True),
        _next_follow_generation=lambda: calls.append('invalidate'),
        _refresh_prompt_controls=lambda: calls.append('refresh'),
        _scroll_down=lambda: calls.append('scroll'),
    )
    LiteTUI.action_toggle_scroll_lock(app)
    assert not app.settings.autoscroll
    assert calls == ['invalidate', 'refresh']
    calls.clear()
    LiteTUI.action_toggle_scroll_lock(app)
    assert app.settings.autoscroll
    assert calls == ['invalidate', 'refresh', 'scroll']
