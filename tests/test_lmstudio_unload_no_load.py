from types import SimpleNamespace
import pytest
from litetui.llm_backend import LMStudioBackend, BackendError
from litetui.lmstudio_session import LMStudioSession
from contextlib import contextmanager


def session_for(client):
    session = LMStudioSession.__new__(LMStudioSession)
    @contextmanager
    def operation():
        yield client
    session._operation = operation
    return session


@pytest.mark.asyncio
async def test_unload_absent_model_never_acquires_or_loads():
    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend._host = 'fixture'
    def forbidden(*args, **kwargs):
        pytest.fail('unload attempted SDK acquire/load')
    backend._sdk = lambda: session_for(SimpleNamespace(llm=forbidden, list_loaded_models=lambda: []))
    await backend.unload('absent')


@pytest.mark.asyncio
async def test_unload_only_exact_loaded_identifier():
    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend._host = 'fixture'
    unloaded = []
    handles = [SimpleNamespace(identifier=key, unload=lambda key=key: unloaded.append(key))
               for key in ('other', 'wanted')]
    backend._sdk = lambda: session_for(SimpleNamespace(list_loaded_models=lambda: handles))
    await backend.unload('wanted')
    assert unloaded == ['wanted']


@pytest.mark.asyncio
async def test_unload_inventory_failure_does_not_fallback_to_load():
    backend = LMStudioBackend.__new__(LMStudioBackend)
    backend._host = 'fixture'
    def failed():
        raise RuntimeError('inventory unavailable')
    backend._sdk = lambda: session_for(SimpleNamespace(list_loaded_models=failed))
    with pytest.raises(BackendError, match='could not unload'):
        await backend.unload('wanted')
