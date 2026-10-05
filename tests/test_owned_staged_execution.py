"""Staged first-use choices publish owned authority before optional snapshots.
Fixture homes only: no provider, process launch, registry registration or live IO.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from litetui import agent_ownership, app as app_module, paths, settings
from litetui.agent_launch_context import ordinary
from litetui.convo_settings import load
from litetui.plugins.model_switch import switch_model


@contextmanager
def staged_app(tmp_path, monkeypatch):
    cfg = settings.Settings(backend='codex', default_model='old-model', thinking_level='high')
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    with ordinary(tmp_path, cfg) as session:
        app = app_module.LiteTUI(agent_session=session)
        app._system = lambda _: None
        app._backend = SimpleNamespace(name='codex', request_overrides=lambda _: {},
                                       reasoning_levels=lambda _: ['medium', 'high'])
        app._model_id = 'old-model'
        app.available_models = ['old-model', 'new-model']
        app._thinking_level = 'high'
        app._cli_effective_thinking = 'high'
        # Provider/UI effects are outside this regression; exercise publication.
        app.update_header = lambda: None
        app.fetch_context_window = lambda: None
        app.system_message = lambda _: None
        app.apply_context_length = lambda: None
        app._rpc_emit_model_state = lambda: None
        assert app._convo_settings is None
        assert app.store.pending
        try:
            yield app, session
        finally:
            app.store.release()


def test_staged_model_choice_survives_first_materialization(tmp_path, monkeypatch):
    with staged_app(tmp_path, monkeypatch) as (app, session):
        assert switch_model(app, 'new-model')
        assert session.authority.model == app.model_id == app.settings.default_model == 'new-model'
        assert app._convo_settings is None
        assert not (app.convo_dir / 'settings.json').exists()
        # Same pre-provider guard that refused Ryan's first hello.
        app_module.LiteTUI._validate_owned_execution(app)
        app._materialise_convo()
        snapshot = load(app.convo_dir)
        assert snapshot.model == snapshot.execution['default_model'] == 'new-model'
        assert snapshot.backend == session.authority.backend == 'codex'
        app_module.LiteTUI._validate_owned_execution(app)


@pytest.mark.parametrize('materialized', [False, True])
def test_same_runtime_reselection_repairs_owned_authority(tmp_path, monkeypatch, materialized):
    with staged_app(tmp_path, monkeypatch) as (app, session):
        if materialized:
            app._materialise_convo()
            # A snapshot saying the selected value is not execution authority.
            app._convo_settings.model = 'new-model'
        app._model_id = 'new-model'  # reproduce the old staged-only runtime divergence
        app._cli_initial_model = 'old-model'
        assert session.authority.model == 'old-model'
        assert switch_model(app, 'new-model')
        assert session.authority.model == app.model_id == 'new-model'
        assert app._cli_initial_model is None
        assert app._user_model_choice == 'new-model'
        app_module.LiteTUI._validate_owned_execution(app)


@pytest.mark.parametrize('field,value,authority_field', [
    ('model_id', 'new-model', 'model'),
    ('thinking_level', 'medium', 'thinking_level'),
    ('backend', SimpleNamespace(name='claude'), 'backend'),
])
def test_staged_execution_setters_publish_without_snapshot(tmp_path, monkeypatch, field, value, authority_field):
    with staged_app(tmp_path, monkeypatch) as (app, session):
        setattr(app, field, value)
        expected = value.name if field == 'backend' else value
        assert getattr(session.authority, authority_field) == expected
        assert app._convo_settings is None
        assert not (app.convo_dir / 'settings.json').exists()


@pytest.mark.parametrize('field,value', [
    ('model_id', 'new-model'),
    ('thinking_level', 'medium'),
    ('backend', SimpleNamespace(name='claude')),
    ('switch_model', 'new-model'),
])
def test_staged_publication_failure_preserves_runtime_and_cli(tmp_path, monkeypatch, field, value):
    with staged_app(tmp_path, monkeypatch) as (app, session):
        app._resume_connection_error = 'retain-error'
        old_backend = app.backend
        before = (app.model_id, app.thinking_level, app._cli_initial_model,
                  app._cli_thinking_level, app._cli_effective_thinking, app._resume_connection_error)
        home_before = (session.memory_root / 'settings.json').read_bytes()
        authority_before = session.authority
        def fail_replace(*_):
            raise OSError('fixture staged publication failure')
        monkeypatch.setattr(agent_ownership.os, 'replace', fail_replace)
        with pytest.raises(OSError, match='staged publication failure'):
            if field == 'switch_model':
                switch_model(app, value)
            else:
                setattr(app, field, value)
        assert app.backend is old_backend
        assert (app.model_id, app.thinking_level, app._cli_initial_model,
                app._cli_thinking_level, app._cli_effective_thinking, app._resume_connection_error) == before
        assert not hasattr(app, '_user_model_choice')
        assert session.authority == authority_before
        assert (session.memory_root / 'settings.json').read_bytes() == home_before
        assert app._convo_settings is None


def test_matching_staged_reselection_does_not_republish(tmp_path, monkeypatch):
    with staged_app(tmp_path, monkeypatch) as (app, session):
        before = (session.memory_root / 'settings.json').read_bytes()
        def unexpected(*_, **__):
            pytest.fail('Matching authority must not be republished')
        monkeypatch.setattr(session, 'update_execution', unexpected)
        assert switch_model(app, 'old-model')
        assert (session.memory_root / 'settings.json').read_bytes() == before
        assert not (app.convo_dir / 'settings.json').exists()
