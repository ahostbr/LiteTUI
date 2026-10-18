"""Historical model config provenance must not mutate another owned home model."""
from types import SimpleNamespace
import pytest
from litetui import app as app_module, settings, paths, convo_settings
from litetui.agent_launch_context import ordinary


@pytest.mark.parametrize('backend,field,map_key', [('llamacpp', 'llama_load', 'llama_load_settings'),
                                                ('lmstudio', 'lmstudio_load', 'lmstudio_load_settings')])
@pytest.mark.parametrize('matches', [False, True])
def test_owned_adopt_load_config_respects_recorded_model(tmp_path, monkeypatch, backend, field, map_key, matches):
    cfg = settings.Settings(backend=backend, default_model='home-model')
    setattr(cfg, map_key, {'unrelated': {'ctx': 512}, 'home-model': {'ctx': 1024}})
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    with ordinary(tmp_path, cfg) as session:
        app = app_module.LiteTUI(agent_session=session)
        app._backend = SimpleNamespace(name=backend)
        app._cli_thinking_level = None
        app._cli_initial_model = None
        app._cli_initial_backend = None
        messages = []
        app._system = messages.append
        record = convo_settings.ConvoSettings(model='home-model' if matches else 'historical-other',
                                             backend=backend, reasoning_effort='xhigh')
        setattr(record, field, {'ctx': 8192})
        convo_settings.save(app.convo_dir, record, agent_session=session)
        before = (app.convo_dir / 'settings.json').read_bytes()
        home_before = (session.memory_root / 'settings.json').read_bytes()
        try:
            app._adopt_convo_settings(born=False)
            actual = getattr(app.settings, map_key)
            assert actual['unrelated'] == {'ctx': 512}
            if matches:
                assert actual['home-model'] == {'ctx': 8192}
            else:
                assert actual['home-model'] == {'ctx': 1024}, 'historical other-model config overwrote existing home config'
                assert any('historical-other' in text and 'not applied' in text for text in messages)
            assert app.model_id == 'home-model'
            assert 'reasoning_effort' not in app.settings.model_infer_overrides.get('home-model', {})
            assert (app.convo_dir / 'settings.json').read_bytes() == before
            assert (session.memory_root / 'settings.json').read_bytes() == home_before
        finally:
            app.store.release()
