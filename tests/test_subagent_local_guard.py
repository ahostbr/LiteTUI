"""Expert local admission reads live status; it never calls any load API."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui.subagent_local import admit_local


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled,states,reason', [
    (False, {'child': 'loaded'}, 'expert'),
    (True, {'child': 'loaded', 'other': 'loading'}, 'loading'),
    (True, {'child': 'loaded', 'other': 'loaded'}, 'different'),
    (True, {'child': 'unloaded'}, 'already loaded'),
    (True, {}, 'already loaded'),
])
async def test_local_refuses_without_loading(enabled, states, reason):
    load = Mock()
    backend = SimpleNamespace(name='lmstudio', subagent_model_states=lambda: states, load=load)
    with pytest.raises(ValueError, match=reason):
        await admit_local(backend, 'child', enabled)
    load.assert_not_called()


@pytest.mark.asyncio
async def test_same_loaded_model_reused_and_status_refreshed():
    states = {'child': 'loaded'}
    backend = SimpleNamespace(name='lmstudio', subagent_model_states=lambda: dict(states), load=Mock())
    await admit_local(backend, 'child', True)
    states['other'] = 'loading'
    with pytest.raises(ValueError, match='loading'):
        await admit_local(backend, 'child', True)
    backend.load.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_status_refuses_fail_closed():
    backend = SimpleNamespace(name='custom', subagent_model_states=lambda: {'child': 'unknown'})
    with pytest.raises(ValueError, match='verify'):
        await admit_local(backend, 'child', True)


def test_lmstudio_status_includes_loading_using_native_endpoint(monkeypatch):
    from litetui.llm_backend import LMStudioBackend
    from litetui.settings import Settings
    backend = LMStudioBackend(Settings())
    monkeypatch.setattr(backend, '_native_models', lambda: [
        {'id': 'child', 'state': 'loaded', 'loaded_context_length': 8192},
        {'id': 'other', 'state': 'loading'},
    ])
    assert backend.subagent_model_states() == {'child': 'loaded', 'other': 'loading'}
