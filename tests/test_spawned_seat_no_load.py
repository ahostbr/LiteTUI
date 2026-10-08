"""T0408-I: a spawned worker seat never sends a request that could load its model.

Dispatch proves "loaded now" once; the seat works for hours. On an engine that
loads whatever a request names (LM Studio, Strata, a local custom server that
reports a state per model) the seat asks again before every turn.

NO MODEL IS LOADED AND NO SERVER IS CONTACTED HERE: every backend is a recording
double, and the one real LM Studio class reads an in-process fake.
"""
from __future__ import annotations

import socket
import urllib.request
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import llm_backend as be
from litetui import paths, settings, subagent_local
from litetui.agent_launch_context import create

MODEL = 'local-fixture-1'
SEAT = '22222222-2222-4222-8222-222222222222'
LOCAL_URL = 'http://127.0.0.1:7470/v1'
PUBLIC_URL = 'https://openrouter.example.com/api/v1'


class Backend:
    """Recording double. `reached` is the turn getting past the seat's check."""
    def __init__(self, name, states, *, url=LOCAL_URL):
        self.touched = []
        self.name, self.states, self.url = name, states, url
        self.queries, self.reached = 0, []

    def base_url(self):
        return self.url

    def subagent_model_states(self):
        self.queries += 1
        if isinstance(self.states, Exception):
            raise self.states
        return dict(self.states)

    def ensure_chat_ready(self, key):
        self.reached.append(key)

    def __getattr__(self, name):
        # Absent, as on a backend without the method; `touched` keeps the evidence.
        self.touched.append(name)
        raise AttributeError(name)


#: What a check that only reads state can never need.
STARTS_OR_LOADS = {'load', 'unload', 'ensure_running', 'list_models', 'loaded_models', 'start_engine',
                   'apply_load_settings', '_sdk', '_spawn'}


def seat(backend, *, spawned=True, tier='worker', cached_loaded=()):
    """A LiteTUI shell at the point a turn is about to be sent."""
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a._rpc = False
    a.model_id = MODEL
    a.backend = backend
    a._agent_session = SimpleNamespace(authority=SimpleNamespace(
        model=MODEL, backend=backend.name, thinking_level='low'))
    a._spawned_seat = spawned
    a.seat = SimpleNamespace(tier=tier, registered=True)
    a.model_rows = {key: be.ModelRow(key, None, 'fixture', loaded=True) for key in cached_loaded}

    async def registered():
        return True
    a._register_owned_startup = registered
    return a


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('a seat check test reached the network')
    # Not socket.connect itself: the event loop's own socketpair needs it on Windows.
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    # Not the subject here: it pins backend, model and effort to the seat's authority.
    monkeypatch.setattr(app_mod.LiteTUI, '_validate_owned_execution', lambda self: None)
    monkeypatch.setattr(subagent_local, 'resolve_host',
                        lambda host: {'openrouter.example.com': ['104.18.2.115']}[host], raising=False)


async def turn(a):
    await app_mod.LiteTUI._ensure_chat_ready(a)


STATEFUL = {'other-model': 'unloaded'}   # a server that DOES report a state per model


@pytest.mark.asyncio
@pytest.mark.parametrize('name,states', [
    ('lmstudio', {}),
    ('lmstudio', {MODEL: 'unloaded'}),
    ('lmstudio', {MODEL: 'loading'}),
    ('lmstudio', {MODEL: 'unknown'}),
    ('lmstudio', {'other-model': 'loaded'}),
    ('lmstudio', be.BackendError('fixture: could not read the model list')),
    ('lmstudio', ConnectionRefusedError('fixture: nothing is listening')),
    ('strata', {}),
    ('strata', {MODEL: 'unloaded'}),
    ('strata', be.BackendError('fixture: no server')),
    ('custom', {MODEL: 'unloaded', **STATEFUL}),
    ('custom', STATEFUL),
])
async def test_a_spawned_worker_refuses_the_turn_when_its_model_is_not_loaded_now(name, states):
    backend = Backend(name, states)
    with pytest.raises(be.BackendError, match='not loaded') as refusal:
        await turn(seat(backend))
    assert 'approval' in str(refusal.value)
    assert backend.reached == [], 'the turn got past the check for a model that is not loaded'
    assert backend.queries == 1, 'exactly one read-only state query per turn'
    assert not STARTS_OR_LOADS & set(backend.touched), backend.touched


@pytest.mark.asyncio
async def test_the_connect_time_listing_cannot_see_an_unload_so_the_seat_asks_again():
    """The listing from connect still says loaded; the server has since unloaded it."""
    backend = Backend('lmstudio', {MODEL: 'unloaded'})
    a = seat(backend, cached_loaded=[MODEL])
    assert MODEL in {row.key for row in a.model_rows.values() if row.loaded}
    with pytest.raises(be.BackendError, match='not loaded'):
        await turn(a)
    assert backend.reached == []


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['lmstudio', 'strata', 'custom'])
async def test_CONTROL_a_loaded_model_passes_and_is_asked_about_every_turn(name):
    """The positive half: the same harness lets a turn through, twice, on two queries."""
    backend = Backend(name, {MODEL: 'loaded', **STATEFUL})
    a = seat(backend)
    await turn(a)
    await turn(a)
    assert backend.reached == [MODEL, MODEL]
    assert backend.queries == 2
    backend.states = {MODEL: 'unloaded', **STATEFUL}   # idle unload between two turns
    with pytest.raises(be.BackendError, match='not loaded'):
        await turn(a)
    assert backend.reached == [MODEL, MODEL]
    assert not STARTS_OR_LOADS & set(backend.touched), backend.touched


@pytest.mark.asyncio
@pytest.mark.parametrize('name,states,url', [
    # Engines that cannot load on a request get no seat check.
    ('llamacpp', {}, LOCAL_URL),
    ('ninfer', {}, LOCAL_URL),
    # Remote by construction.
    ('claude', {}, LOCAL_URL),
    ('codex', {}, LOCAL_URL),
    ('cline', {}, LOCAL_URL),
    ('free', {}, LOCAL_URL),
])
async def test_CONTROL_other_backends_are_not_asked_and_behave_as_before(name, states, url):
    backend = Backend(name, states, url=url)
    await turn(seat(backend))
    assert backend.reached == [MODEL]
    assert backend.queries == 0


@pytest.mark.asyncio
async def test_CONTROL_a_custom_server_at_a_public_address_is_never_asked():
    backend = Backend('custom', {MODEL: 'unloaded', **STATEFUL}, url=PUBLIC_URL)
    await turn(seat(backend))
    assert backend.reached == [MODEL] and backend.queries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('states', [
    {MODEL: 'unknown'},                          # a plain server: no state on any row
    {MODEL: 'unknown', 'other-model': 'loaded'},  # one row without a state
    {},                                          # lists nothing
    be.BackendError('fixture: not answering'),   # cannot be read at all
])
async def test_CONTROL_a_local_custom_server_that_reports_no_state_is_left_as_today(states):
    """Dispatch never accepts such a server, and seats people run on them keep working."""
    backend = Backend('custom', states)
    await turn(seat(backend))
    assert backend.reached == [MODEL]


@pytest.mark.asyncio
@pytest.mark.parametrize('spawned,tier', [
    (False, 'worker'),        # a seat a person launched: it keeps the load on its first turn
    (True, 'leader'),
    (True, 'thinker'),
    (True, 'reviewer'),
    (True, 'orchestrator'),
])
async def test_CONTROL_only_a_spawned_worker_is_checked(spawned, tier):
    backend = Backend('lmstudio', {})
    await turn(seat(backend, spawned=spawned, tier=tier))
    assert backend.reached == [MODEL], 'a seat that is not a spawned worker stopped reaching its backend'
    assert backend.queries == 0


def test_lmstudio_reports_a_downloaded_model_that_is_not_loaded_as_unloaded(monkeypatch):
    """LM Studio's own word for it is `not-loaded`; the shared predicate knows `unloaded`."""
    backend = be.LMStudioBackend(settings.Settings())
    monkeypatch.setattr(backend, '_native_models', lambda: [
        {'id': MODEL, 'state': 'loaded', 'loaded_context_length': 8192},
        {'id': 'other-model', 'state': 'not-loaded'},
    ])
    assert backend.subagent_model_states() == {MODEL: 'loaded', 'other-model': 'unloaded'}
    subagent_local.require_sole_resident(backend.subagent_model_states(), MODEL)


# ── the connect of a dispatched seat sends no chat request ──────────────────

def connected_seat(tmp_path, monkeypatch, session):
    """A real app built from the flags dispatch passes, on the real LM Studio class."""
    cfg = settings.Settings(default_model=MODEL)
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    app = app_mod.LiteTUI(agent_session=session, initial_backend='lmstudio', initial_model=MODEL,
                          initial_thinking='low', spawn_identity=(SEAT, 'Gamma-Fixture', 'worker'))
    seen = SimpleNamespace(requests=[], probes=[], lines=[])

    def http(url, body=None, timeout=10.0):
        seen.requests.append(('GET' if body is None else 'POST', url))
        assert body is None, 'connect POSTed to the model server'
        assert url.endswith('/api/v0/models'), url
        return {'data': [{'id': MODEL, 'state': 'not-loaded'}]}   # downloaded, NOT loaded

    monkeypatch.setattr(be, '_http_json', http)
    monkeypatch.setattr(app_mod.thinking_probe, 'get_effective_levels',
                        lambda *args, **kwargs: seen.probes.append('chat request') or ['off'])
    app._probe_thinking = lambda: seen.probes.append('thinking probe')
    app._system = seen.lines.append
    app._update_header = lambda: None
    app._fetch_ctx_window = lambda: None
    app._rpc_emit_model_state = lambda: None
    app.call_after_refresh = lambda *args, **kwargs: None
    return app, seen


@pytest.mark.asyncio
@pytest.mark.parametrize('thinking,probes', [('low', []), (None, ['thinking probe'])],
                         ids=['dispatched-seat', 'CONTROL-no-launch-thinking-level'])
async def test_a_dispatched_seats_connect_sends_no_chat_request(tmp_path, monkeypatch, thinking, probes):
    """Dispatch always passes a thinking level, and that alone keeps the LM Studio
    thinking probe (a chat request naming the model) from firing at connect. The
    second case removes it and the same harness sees the probe."""
    with create(tmp_path, 'Gamma-Fixture', agent_id=SEAT, backend='lmstudio',
                model=MODEL, thinking_level='low') as session:
        app, seen = connected_seat(tmp_path, monkeypatch, session)
        try:
            assert app._spawned_seat is True and app.seat.tier == 'worker'
            assert app._cli_thinking_level == 'low'
            assert isinstance(app.backend, be.LMStudioBackend)
            app._cli_thinking_level = thinking
            await app_mod.LiteTUI.connect.__wrapped__(app)
            assert app._gui_connection_success is True, seen.lines
            assert f'Connected — model: {MODEL}' in seen.lines, seen.lines
            assert seen.probes == probes
            assert seen.requests and all(method == 'GET' and url.endswith('/api/v0/models')
                                         for method, url in seen.requests), seen.requests
        finally:
            app.store.release()
