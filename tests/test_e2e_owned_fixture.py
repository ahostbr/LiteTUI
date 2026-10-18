"""Hermetic validation of live-probe ownership fixtures; no e2e imports/runs."""
import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from litetui import paths, settings
from litetui.agent_launch_context import acquire

HELPER = Path(__file__).resolve().parents[1] / 'e2e' / '_owned_app.py'
spec = importlib.util.spec_from_file_location('owned_e2e_fixture', HELPER)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


@pytest.mark.parametrize('failure', ['constructor', 'body', 'success'])
def test_owned_fixture_restores_roots_and_releases_on_every_exit(tmp_path, monkeypatch, failure):
    old_load, old_path, old_convos = settings.load, settings.settings_path, paths.CONVO_DIR
    old_env = os.environ.get('LITETUI_DATA_ROOT')
    monkeypatch.setenv('LITETUI_NO_HARNESS', '1')
    cfg = settings.Settings(backend='codex', default_model='fixture-model', thinking_level='high')
    released = []
    rpc_start = object()
    try:
        with fixture.owned_session(tmp_path, cfg) as session:
            assert paths.data_root() == tmp_path
            assert session.authority.model == 'fixture-model'
            assert session.authority.thinking_level == 'high'
            if failure == 'constructor':
                raise RuntimeError('constructor')
            app = SimpleNamespace(_agent_session=session, seat=SimpleNamespace(registered=False),
                                  store=SimpleNamespace(release=lambda: released.append('store')),
                                  _local_rpc=SimpleNamespace(start=rpc_start))
            fixture.configure_owned_app(app)
            assert app.seat.register() is True
            assert app.seat.poll() == []
            assert app._local_rpc.start is rpc_start  # bridge under test is real
            if failure == 'body':
                raise RuntimeError('body')
    except RuntimeError as exc:
        assert str(exc) == failure
    assert released == ([] if failure == 'constructor' else ['store'])
    assert settings.load is old_load and settings.settings_path is old_path
    assert paths.CONVO_DIR == old_convos
    assert os.environ.get('LITETUI_DATA_ROOT') == old_env
    from litetui.agent_ownership import OwnershipError
    with pytest.raises(OwnershipError, match='not owned'):
        session.authority
    reopened = acquire(tmp_path, 'LiteTUI')
    try:
        assert reopened.authority.model == 'fixture-model'
    finally:
        reopened.release()


def test_fixture_initial_execution_is_exact_and_existing_divergence_refuses(tmp_path):
    cfg = settings.Settings(backend='codex', default_model='fixture-model', thinking_level='high')
    with fixture.owned_session(tmp_path, cfg, backend='claude', model='sonnet', thinking_level='low') as session:
        assert (session.authority.backend, session.authority.model, session.authority.thinking_level) == ('claude', 'sonnet', 'low')
    with pytest.raises(ValueError, match='disagree'):
        with fixture.owned_session(tmp_path, cfg, model='other'):
            pytest.fail('must not silently change existing execution')


def test_all_ten_e2e_direct_app_callers_hold_explicit_session():
    root = HELPER.parent
    calls = []
    for path in root.glob('*.py'):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ((isinstance(node.func, ast.Name) and node.func.id == 'LiteTUI') or
                    (isinstance(node.func, ast.Attribute) and node.func.attr == 'LiteTUI')):
                assert any(k.arg == 'agent_session' for k in node.keywords), path
                enclosing = [n for n in ast.walk(tree) if isinstance(n, ast.With) and n.lineno < node.lineno <= n.end_lineno
                             and any('owned_session' in ast.unparse(i.context_expr) for i in n.items)]
                assert enclosing, path
                calls.append(path.name)
    assert len(calls) == 10


def test_direct_script_helper_fallback_import_without_live_module_execution(monkeypatch):
    import sys
    import builtins
    tree = ast.parse((HELPER.parent / 'claude_cwd_smoke.py').read_bytes())
    block = next(n for n in tree.body if isinstance(n, ast.Try)
                 and 'from e2e._owned_app' in ast.unparse(n))
    original_import = builtins.__import__
    def script_import(name, *args, **kwargs):
        if name == 'e2e._owned_app':
            raise ModuleNotFoundError("No module named 'e2e'", name='e2e')
        return original_import(name, *args, **kwargs)
    monkeypatch.syspath_prepend(str(HELPER.parent))
    monkeypatch.setattr(builtins, '__import__', script_import)
    namespace = {}
    exec(compile(ast.Module(body=[block], type_ignores=[]), '<e2e-helper-fallback>', 'exec'), namespace)
    assert callable(namespace['owned_session'])
    assert callable(namespace['configure_owned_app'])
