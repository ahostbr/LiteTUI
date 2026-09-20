import sys
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest


def fake_sdk(monkeypatch):
    sdk = SimpleNamespace(timeout=60, clients=[], calls=[])
    class Client:
        def __init__(self, host):
            self.host = host
            self.closed = False
            self.llm = SimpleNamespace(model=self.model)
            sdk.clients.append(self)
        def model(self, key, config=None):
            sdk.calls.append((self.host, key, config, sdk.timeout))
            if key == 'fail':
                raise RuntimeError('load fixture')
        def list_loaded_models(self):
            return [SimpleNamespace(identifier='loaded', unload=lambda: sdk.calls.append((self.host, 'unload', sdk.timeout)))]
        def close(self):
            self.closed = True
    sdk.Client = Client
    sdk.get_sync_api_timeout = lambda: sdk.timeout
    def set_timeout(value):
        sdk.timeout = value
    sdk.set_sync_api_timeout = set_timeout
    monkeypatch.setitem(sys.modules, 'lmstudio', sdk)
    return sdk


def test_explicit_clients_route_independently_and_restore_timeout(monkeypatch):
    from litetui.lmstudio_session import LMStudioSession
    sdk = fake_sdk(monkeypatch)
    a = LMStudioSession('host-a:1234', timeout=111)
    b = LMStudioSession('host-b:1234', timeout=222)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda session: session.load('model', config={'contextLength': 8192}), [a, b]))
    assert sorted(sdk.calls) == [('host-a:1234', 'model', {'contextLength': 8192}, 111), ('host-b:1234', 'model', {'contextLength': 8192}, 222)]
    assert sdk.timeout == 60
    a.close()
    assert sdk.clients[0].closed
    assert not sdk.clients[1].closed
    b.load('still-live')
    a.close()
    with pytest.raises(RuntimeError, match='closed'):
        a.load('no-reopen')
    b.close()


def test_failure_restores_timeout_and_unload_never_loads(monkeypatch):
    from litetui.lmstudio_session import LMStudioSession
    sdk = fake_sdk(monkeypatch)
    session = LMStudioSession('host:1234', timeout=777)
    with pytest.raises(RuntimeError, match='load fixture'):
        session.load('fail')
    assert sdk.timeout == 60
    sdk.calls.clear()
    session.unload('absent')
    assert not sdk.calls
    session.unload('loaded')
    assert sdk.calls == [('host:1234', 'unload', 777)]
    assert sdk.timeout == 60
    session.close()

@pytest.mark.asyncio
async def test_production_backends_use_owned_sessions(monkeypatch):
    import asyncio
    from litetui.llm_backend import LMStudioBackend
    sdk = fake_sdk(monkeypatch)
    a = LMStudioBackend(SimpleNamespace(lm_host='http://host-a:1234', lms_load_timeout_s=111))
    b = LMStudioBackend(SimpleNamespace(lm_host='http://host-b:1234', lms_load_timeout_s=222))
    await asyncio.gather(a.load('alpha'), b.load('beta'))
    assert {(host, key, timeout) for host, key, cfg, timeout in sdk.calls} == {
        ('host-a:1234', 'alpha', 111), ('host-b:1234', 'beta', 222)}
    a_client = a._sdk_session._client
    b_client = b._sdk_session._client
    a.shutdown()
    assert a_client.closed and not b_client.closed
    await b.load('after-a-exit')
    assert sdk.calls[-1][0:2] == ('host-b:1234', 'after-a-exit')
    b.shutdown()
    assert b_client.closed
    assert sdk.timeout == 60


def test_close_is_bounded_while_load_is_in_flight(monkeypatch):
    from threading import Event, Thread
    from litetui.lmstudio_session import LMStudioSession
    sdk = fake_sdk(monkeypatch)
    session = LMStudioSession('host:1234', timeout=777)
    session.load('initialize')
    entered, release = Event(), Event()
    client = session._client
    def loading(*args, **kwargs):
        entered.set()
        assert release.wait(3)
    client.llm.model = loading
    worker = Thread(target=lambda: session.load('slow'))
    worker.start()
    assert entered.wait(2)
    try:
        assert session.close(timeout=0.02) is False
        assert not client.closed
        assert not session.wait_closed(timeout=0.01)
    finally:
        release.set()
        worker.join(2)
    assert session.wait_closed(timeout=2)
    assert client.closed
    assert sdk.timeout == 60
    with pytest.raises(RuntimeError, match='closed'):
        session.load('no-reopen')


def test_close_failure_is_observable(monkeypatch):
    from litetui.lmstudio_session import LMStudioSession
    sdk = fake_sdk(monkeypatch)
    session = LMStudioSession('host:1234', timeout=777)
    session.load('initialize')
    def failed():
        raise RuntimeError('close fixture')
    session._client.close = failed
    assert session.close(timeout=1) is False
    assert session.wait_closed(timeout=1) is False
    assert 'close fixture' in session.cleanup_error
    with pytest.raises(RuntimeError, match='closed'):
        session.load('no-reopen')
