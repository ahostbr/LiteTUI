"""Defense in depth at shipped seams, not a production exploit claim.
Real owned App hosts; only provider/harness external boundaries are substituted.
"""
from types import SimpleNamespace
import pytest

from litetui import app as app_module, paths, settings, convo_settings
from litetui.agent_launch_context import ordinary
from litetui.agent_store import StoreError


def snapshot(session):
    home = session.memory_root
    return (session.authority, sorted(str(p.relative_to(home)) for p in home.rglob('*')),
            {str(p.relative_to(home)): p.read_bytes() for p in home.rglob('*')
             if p.is_file() and p.name not in ('.agent.lease', '.session.lease')})


@pytest.mark.parametrize('kwargs', [{'initial_model': 'different'}, {'initial_backend': 'codex'},
                                   {'initial_thinking': 'high'}])
def test_real_constructor_refuses_divergent_execution(tmp_path, monkeypatch, kwargs):
    cfg = settings.Settings(default_model='home-model')
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    with ordinary(tmp_path, cfg) as session:
        before = snapshot(session)
        with pytest.raises(StoreError, match='disagrees'):
            app_module.LiteTUI(agent_session=session, **kwargs)
        assert snapshot(session) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['adopt', 'cli'])
@pytest.mark.parametrize('field,value', [('_cli_initial_model', 'different'),
                                      ('_cli_initial_backend', 'codex'),
                                      ('_cli_thinking_level', 'high')])
async def test_secondary_execution_seams_refuse_before_mutation_or_prompt(tmp_path, monkeypatch, method, field, value):
    cfg = settings.Settings(default_model='home-model')
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    with ordinary(tmp_path, cfg) as session:
        app = app_module.LiteTUI(agent_session=session)
        events = []
        app._system = events.append
        app._connect = lambda: events.append('CONNECT')
        app._backend = SimpleNamespace(name='lmstudio')
        app.seat.registered = True  # external registration already acknowledged fixture
        app.available_models = ['different', 'home-model']
        from litetui.llm_backend import ModelRow
        app.model_rows = {name: ModelRow(name, None, 'fixture', loaded=True) for name in app.available_models}
        app.update_header = lambda: None
        app.fetch_context_window = lambda: None
        app._submit_text = lambda *args, **kwargs: events.append('PROMPT')
        app._startup_adopting = True
        app._first_prompt = None if method == 'adopt' else 'NEVER DISPATCH'
        app._cli_thinking_level = None
        app._model_id = 'runtime-before'
        app._thinking_level = 'before'
        async def ready(*args, **kwargs):
            return True  # fake provider readiness; ownership/registration remains real
        monkeypatch.setattr(app, '_ensure_chat_ready', ready)
        app._materialise_convo()
        before = snapshot(session)
        runtime_before = (app.backend, app.model_id, app.thinking_level, vars(app.settings).copy())
        setattr(app, field, value)
        try:
            if method == 'adopt':
                with pytest.raises(StoreError, match='disagrees'):
                    app._adopt_convo_settings(born=False)
            else:
                await app_module.LiteTUI._apply_cli_args.__wrapped__(app)
                assert app._cli_launch_error and 'disagrees' in app._cli_launch_error
            assert (app.backend, app.model_id, app.thinking_level, vars(app.settings)) == runtime_before
            assert 'PROMPT' not in events and 'CONNECT' not in events
            assert snapshot(session) == before
        finally:
            app.store.release()


@pytest.mark.asyncio
async def test_matching_cli_execution_is_accepted_and_dispatches_without_persistence(tmp_path, monkeypatch):
    cfg = settings.Settings(default_model='home-model')
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    with ordinary(tmp_path, cfg) as session:
        app = app_module.LiteTUI(agent_session=session, initial_model='home-model', initial_backend='lmstudio')
        events = []
        app._system = events.append
        app.seat.registered = True
        app.available_models = ['home-model']
        from litetui.llm_backend import ModelRow
        app.model_rows = {'home-model': ModelRow('home-model', None, 'fixture', loaded=True)}
        app.update_header = lambda: None
        app.fetch_context_window = lambda: None
        app._cli_thinking_level = None
        app._first_prompt = 'POSITIVE'
        app._submit_text = lambda *args, **kwargs: events.append('PROMPT')
        async def ready(*args, **kwargs):
            return True  # fake provider readiness, not ownership/CLI validation
        monkeypatch.setattr(app, '_ensure_chat_ready', ready)
        app._materialise_convo()
        before = snapshot(session)
        try:
            await app_module.LiteTUI._apply_cli_args.__wrapped__(app)
            assert app._cli_launch_error is None
            assert events.count('PROMPT') == 1
            assert app.model_id == 'home-model'
            assert snapshot(session) == before
        finally:
            app.store.release()


@pytest.mark.parametrize('route', ['setter', 'command'])
def test_normal_backend_choice_then_new_child_and_adopt_is_valid(tmp_path, monkeypatch, route):
    cfg = settings.Settings(default_model='home-model')
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    with ordinary(tmp_path, cfg) as session:
        app = app_module.LiteTUI(agent_session=session)
        app._system = lambda text: None
        app.system_message = lambda text: None
        app.update_header = lambda: None
        app.connect = lambda: None
        app._backend = SimpleNamespace(name='lmstudio')
        monkeypatch.setattr(app_module.llm_backend, 'make_backend', lambda s: SimpleNamespace(name=s.backend))
        app._materialise_convo()
        try:
            if route == 'setter':
                app.backend = SimpleNamespace(name='codex')
            else:
                from litetui.plugins import model_switch
                model_switch._switch_backend(app, 'codex')
            assert session.authority.backend == 'codex'
            app.thinking_level = 'high'
            assert session.authority.thinking_level == 'high'
            app.model_id = 'new-model'
            assert session.authority.model == 'new-model'
            app._new_convo()
            app._materialise_convo()
            app._adopt_convo_settings(born=False)
            assert app.backend.name == 'codex'
            assert app.thinking_level == 'high'
            assert session.authority.backend == 'codex'
        finally:
            app.store.release()
