from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from litetui.plugins import monitor_plugin as plugin


def test_monitor_thread_owned_before_start_and_cleared_after_exit(monkeypatch):
    app = SimpleNamespace()
    threads = []
    class Thread:
        def __init__(self, **kwargs): self.target = kwargs['target']; threads.append(self)
        def start(self): assert self in app._monitor_threads
        def is_alive(self): return True
    monkeypatch.setattr(plugin.threading, 'Thread', Thread)
    monkeypatch.setattr(plugin, '_run_sweep_real', Mock())
    plugin._handle(app, '/monitor', 'run')
    assert threads[0] in app._monitor_threads
    threads[0].target()
    assert not app._monitor_threads


def test_failed_thread_start_does_not_leave_false_busy(monkeypatch):
    app = SimpleNamespace()
    class Thread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('cannot start')
    monkeypatch.setattr(plugin.threading, 'Thread', Thread)
    with pytest.raises(RuntimeError, match='cannot start'):
        plugin._handle(app, '/monitor', 'run')
    assert not app._monitor_threads


def test_failed_sweep_releases_owned_observer(monkeypatch):
    app = SimpleNamespace()
    threads = []
    class Thread:
        def __init__(self, **kwargs): self.target = kwargs['target']; threads.append(self)
        def start(self): pass
    monkeypatch.setattr(plugin.threading, 'Thread', Thread)
    monkeypatch.setattr(plugin, '_run_sweep_real', Mock(side_effect=OSError('probe failed')))
    monkeypatch.setattr(plugin, '_post', Mock())
    plugin._handle(app, '/monitor', 'run')
    threads[0].target()
    assert not app._monitor_threads
