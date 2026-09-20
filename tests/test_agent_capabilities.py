from types import SimpleNamespace
import pytest
from litetui.agent_capabilities import hosted_levels
from litetui.agent_launcher import LaunchBlocked


def test_connected_catalogue_used_without_changing_parent():
    levels = ['low', 'high']
    backend = SimpleNamespace(name='codex', models={'chosen': {}}, reasoning_levels=lambda model: levels)
    app = SimpleNamespace(backend=backend, model_id='parent-model')
    result = hosted_levels(app, {'backend': 'codex', 'model': 'chosen'})
    assert result == levels
    result.append('other')
    assert levels == ['low', 'high']
    assert app.model_id == 'parent-model'


@pytest.mark.parametrize('backend,model', [('ninfer', 'chosen'), ('codex', 'unknown')])
def test_no_cross_backend_or_unknown_model_fallback(backend, model):
    app = SimpleNamespace(backend=SimpleNamespace(name='codex', models={'chosen': {}},
                                                  reasoning_levels=lambda model: ['low']))
    with pytest.raises(LaunchBlocked): hosted_levels(app, {'backend': backend, 'model': model})
