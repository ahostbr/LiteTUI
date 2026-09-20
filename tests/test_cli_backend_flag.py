import sys
from types import SimpleNamespace
import pytest


def test_cli_passes_backend_without_mutating_environment(tmp_path, monkeypatch):
    from litetui import cli, paths, shared_state
    seen = {}
    class App:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def run(self, **kwargs):
            seen['ran'] = True
    monkeypatch.setitem(sys.modules, 'litetui.app', SimpleNamespace(LiteTUI=App, wants_ansi_fallback=lambda: False))
    monkeypatch.setitem(sys.modules, 'litetui.image_viewer', SimpleNamespace(init_image_backend=lambda: None))
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(shared_state, 'check_data_version', lambda root: None)
    monkeypatch.setenv('LITETUI_BACKEND', 'ninfer')
    monkeypatch.setattr(sys, 'argv', ['litetui', '--backend', 'codex'])
    cli.main()
    assert seen['initial_backend'] == 'codex'
    import os
    assert os.environ['LITETUI_BACKEND'] == 'ninfer'
    assert seen['ran']


def test_invalid_backend_rejected_before_app_import(monkeypatch):
    from litetui import cli
    class ForbiddenApp:
        def __init__(self, **kwargs):
            pytest.fail('invalid backend reached app construction')
    monkeypatch.setitem(sys.modules, 'litetui.app', SimpleNamespace(LiteTUI=ForbiddenApp, wants_ansi_fallback=lambda: False))
    monkeypatch.setattr(sys, 'argv', ['litetui', '--backend', 'invalid'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2


def test_conflicting_thinking_flags_rejected_before_app(monkeypatch):
    from litetui import cli
    class ForbiddenApp:
        def __init__(self, **kwargs):
            pytest.fail('conflicting flags reached app')
    monkeypatch.setitem(sys.modules, 'litetui.app', SimpleNamespace(LiteTUI=ForbiddenApp, wants_ansi_fallback=lambda: False))
    monkeypatch.setattr(sys, 'argv', ['litetui', '--reasoning-effort', 'high', '--thinking-level', 'low'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
