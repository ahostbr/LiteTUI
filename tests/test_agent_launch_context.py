"""Owned bootstrap and actual lifecycle seams; fixture stores, no providers."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from litetui import agent_launch_context, agent_ownership, agent_store
from litetui.app import LiteTUI, ConversationRepository
from litetui.settings import Settings

AID = '11111111-1111-4111-8111-111111111111'
CID = '33333333-3333-4333-8333-333333333333'
DID = '44444444-4444-4444-8444-444444444444'


@pytest.fixture
def root(tmp_path):
    directory = tmp_path / '.agents' / 'QuietHelm'
    directory.mkdir(parents=True)
    (directory / 'settings.json').write_text(json.dumps({
        'schema_version': 1, 'name': directory.name, 'agent_id': AID,
        'execution': {'backend': 'codex', 'model': 'fixture', 'thinking_level': 'high'},
    }))
    return tmp_path


@pytest.fixture
def owned(root):
    with agent_launch_context.acquire(root, 'QuietHelm') as session:
        yield session


@pytest.mark.parametrize('overrides', [dict(backend='lmstudio'), dict(model='wrong'),
                                      dict(thinking_level='low'), dict(conversation_id=CID)])
def test_bootstrap_rejects_divergent_override_or_absent_convo_releases(root, overrides):
    with pytest.raises(agent_store.StoreError):
        agent_launch_context.acquire(root, 'QuietHelm', **overrides)
    with agent_launch_context.acquire(root, 'QuietHelm') as session:
        assert session.authority.agent_id == AID
    assert not (root / '.convos').exists()
    assert not (root / '.agents' / 'QuietHelm' / 'conversations').exists()


def test_bootstrap_matching_flags_settings_win_without_persistence(owned):
    settings = Settings()
    settings.backend, settings.default_model, settings.thinking_level = 'lmstudio', 'poison', 'low'
    before = (owned.memory_root / 'settings.json').read_bytes()
    agent_launch_context.apply_settings(owned, settings)
    assert (settings.backend, settings.default_model, settings.thinking_level) == ('codex', 'fixture', 'high')
    assert settings.seat_name == 'QuietHelm'
    assert (owned.memory_root / 'settings.json').read_bytes() == before


def registration_host(owned, *, succeeds):
    calls = []
    seat = SimpleNamespace(registered=False, error='fixture refusal')
    def register():
        calls.append('register')
        seat.registered = succeeds
        return succeeds
    seat.register = register
    app = SimpleNamespace(_agent_session=owned, seat=seat, _owned_registration_lock=asyncio.Lock(),
                          _owned_launch_error=None, _cli_launch_error=None,
                          store=SimpleNamespace(release=lambda: calls.append('conversation-release')))
    return app, calls


@pytest.mark.asyncio
async def test_failed_register_releases_agent_once_and_stays_blocked(owned):
    app, calls = registration_host(owned, succeeds=False)
    before = (owned.memory_root / 'settings.json').read_bytes()
    assert not await LiteTUI._register_owned_startup(app)
    assert calls == ['register', 'conversation-release']
    assert app._cli_launch_error == 'Owned agent registration blocked: owned-registration-failed; see runtime-errors.log'
    assert not await LiteTUI._register_owned_startup(app)
    assert calls == ['register', 'conversation-release']
    with agent_launch_context.acquire(owned.store.data_root, 'QuietHelm') as new:
        assert new.authority.agent_id == AID
        assert (new.memory_root / 'settings.json').read_bytes() == before


@pytest.mark.asyncio
async def test_ready_and_inbox_share_one_registration_attempt(owned):
    app, calls = registration_host(owned, succeeds=True)
    assert await asyncio.gather(LiteTUI._register_owned_startup(app), LiteTUI._register_owned_startup(app)) == [True, True]
    assert calls == ['register']
    assert owned.authority.agent_id == AID


def test_actual_resume_foreign_path_refuses_before_read_or_hooks(owned, monkeypatch, tmp_path):
    app = SimpleNamespace(_agent_session=owned, _said=[])
    app._system = app._said.append
    monkeypatch.setattr(ConversationRepository, 'read', lambda *_: pytest.fail('read before ownership confinement'))
    assert not LiteTUI._resume(app, tmp_path / 'foreign' / CID / 'convo.jsonl')
    assert app._said == ['Resume target is outside the selected agent']
    assert not (tmp_path / 'foreign').exists()


def test_actual_resume_meta_identity_mismatch_refuses_before_lease_or_hooks(owned, monkeypatch):
    target = owned.conversation_directory(CID) / 'convo.jsonl'
    monkeypatch.setattr(ConversationRepository, 'read', lambda *_: ({'id': DID}, [{'role': 'user', 'content': 'fixture'}]))
    app = SimpleNamespace(_agent_session=owned, _said=[])
    app._system = app._said.append
    assert not LiteTUI._resume(app, target)
    assert app._said == ['Transcript identity disagrees with its owned conversation folder']
    assert not target.parent.exists()


def test_actual_resume_adoption_agent_fields_beat_legacy_and_env(owned, monkeypatch):
    from litetui import convo_settings as cs_mod
    settings = Settings()
    cs = cs_mod.ConvoSettings(backend='lmstudio', model='poison', thinking_level='low')
    cs.execution = {'backend': 'lmstudio', 'default_model': 'poison', 'thinking_level': 'low'}
    monkeypatch.setattr(cs_mod, 'load', lambda *_: cs)
    monkeypatch.setenv('LITETUI_BACKEND', 'lmstudio')
    monkeypatch.setenv('LITETUI_MODEL', 'poison-env')
    monkeypatch.setenv('LITETUI_THINKING', 'low')
    adopted = []
    app = SimpleNamespace(_agent_session=owned, settings=settings, convo_dir=owned.conversation_directory(CID),
        _convo_settings=None, _launch_overrides={}, _invocation_saved_values={}, available_models=[],
        _backend=SimpleNamespace(name='codex'), _cli_initial_backend=None, _cli_tool_profile='interactive',
        _system=lambda _: None, _adopt_convo_backend=lambda value: adopted.append(value))
    LiteTUI._adopt_convo_settings(app, born=False)
    assert (app.settings.backend, app.settings.default_model, app.settings.thinking_level) == ('codex', 'fixture', 'high')
    assert (adopted[0].backend, adopted[0].model, adopted[0].thinking_level) == ('codex', 'fixture', 'high')
    assert (app._model_id, app._thinking_level) == ('fixture', 'high')
    assert not app.convo_dir.exists()


@pytest.mark.parametrize('failure', ['version', 'constructor', 'run'])
def test_actual_cli_exception_paths_release_owned_agent(root, monkeypatch, failure):
    import sys
    from litetui import cli, paths, settings, shared_state, image_viewer
    import litetui.app as app_module
    monkeypatch.setattr(paths, 'data_root', lambda: root)
    monkeypatch.setattr(settings, 'load', Settings)
    monkeypatch.setattr(sys, 'argv', ['litetui', '--agent', 'QuietHelm'])
    observed = []
    original = agent_launch_context.acquire
    def acquire(*args, **kwargs):
        session = original(*args, **kwargs)
        observed.append(session)
        return session
    monkeypatch.setattr(agent_launch_context, 'acquire', acquire)
    def check(_):
        if failure == 'version':
            raise ValueError('fixture version refusal')
    monkeypatch.setattr(shared_state, 'check_data_version', check)
    class FixtureApp:
        def __init__(self, **kwargs):
            assert kwargs['agent_session'].authority.agent_id == AID
            if failure == 'constructor':
                raise RuntimeError('fixture constructor failure')
        def run(self):
            raise RuntimeError('fixture run failure')
    monkeypatch.setattr(app_module, 'LiteTUI', FixtureApp)
    monkeypatch.setattr(app_module, 'wants_ansi_fallback', lambda: False)
    monkeypatch.setattr(image_viewer, 'init_image_backend', lambda: None)
    with pytest.raises(SystemExit if failure == 'version' else RuntimeError):
        cli.main()
    assert len(observed) == 1
    with pytest.raises(agent_ownership.OwnershipError):
        observed[0].authority
    with original(root, 'QuietHelm') as recovered:
        assert recovered.authority.agent_id == AID
    assert not (root / '.convos').exists()


@pytest.mark.asyncio
async def test_later_prompt_is_blocked_after_registration_failure_before_provider(owned):
    from litetui.llm_backend import BackendError
    app, calls = registration_host(owned, succeeds=False)
    app._register_owned_startup = lambda: LiteTUI._register_owned_startup(app)
    with pytest.raises(BackendError, match='owned-registration-failed'):
        await LiteTUI._ensure_chat_ready(app)
    assert calls == ['register', 'conversation-release']


@pytest.mark.asyncio
async def test_owned_headless_turn_refuses_model_substitution(owned):
    from litetui.llm_backend import BackendError
    app, calls = registration_host(owned, succeeds=True)
    app._register_owned_startup = lambda: LiteTUI._register_owned_startup(app)
    app._rpc = True
    app.model_id = 'fixture'
    app._thinking_level = 'high'
    app.backend = SimpleNamespace(name='codex')
    app._headless_model_decision = lambda: ('substitute', 'wrong-model', 'legacy default substitution')
    events = []
    app._rpc_emit = events.append
    with pytest.raises(BackendError, match='no identity fallback'):
        await LiteTUI._ensure_chat_ready(app)
    assert calls == ['register']
    assert events[0]['kind'] == 'model_not_loaded'


@pytest.mark.asyncio
@pytest.mark.parametrize('succeeds', [False, True])
async def test_actual_rpc_ready_reports_registration_or_substitution_blocked(owned, monkeypatch, succeeds):
    from litetui import task_supervisor
    app, calls = registration_host(owned, succeeds=succeeds)
    app._register_owned_startup = lambda: LiteTUI._register_owned_startup(app)
    app._rpc = True
    app.available_models = ['fixture']
    app._headless_model_decision = lambda: ('substitute', 'wrong-model', 'legacy default substitution')
    app._model_id = 'fixture'
    app.convo_id = CID
    app.backend = SimpleNamespace(name='codex', base_url=lambda: '')
    app.ctx_max = None
    app._rpc_model_state = lambda: {'model': app._model_id}
    events = []
    app._rpc_emit = events.append
    monkeypatch.setattr(task_supervisor, 'process_creation_identity', lambda _: 'fixture')
    await LiteTUI._rpc_emit_ready.__wrapped__(app)
    assert calls == (['register'] if succeeds else ['register', 'conversation-release'])
    assert events[0]['type'] == 'ready' and events[0]['launch_status'] == 'blocked'
    assert events[0]['model'] == 'fixture'
    assert app._rpc_ready_sent


def test_pending_new_conversation_reads_agent_memory_without_materializing(owned):
    (owned.memory_root / 'memory.md').write_text('persistent agent note')
    app = SimpleNamespace(_agent_session=owned, convo_dir=owned.conversation_directory(CID), _convo_pending=True)
    assert LiteTUI._read_store_file(app, 'memory.md', 1000) == 'persistent agent note'
    assert not app.convo_dir.exists()
    legacy = SimpleNamespace(convo_dir=app.convo_dir, _convo_pending=True)
    assert LiteTUI._read_store_file(legacy, 'memory.md', 1000) == ''


def test_invalid_owned_constructor_fails_before_hook_startup(owned, monkeypatch):
    from litetui import hook_host
    owned.release()
    monkeypatch.setattr(hook_host, 'initialize', lambda *_: pytest.fail('hook before ownership'))
    with pytest.raises(agent_ownership.OwnershipError):
        LiteTUI(agent_session=owned)


@pytest.mark.asyncio
async def test_cancelled_registration_joins_thread_before_retry_and_shutdown(owned):
    import threading
    entered, finish = threading.Event(), threading.Event()
    app, calls = registration_host(owned, succeeds=True)
    original = app.seat.register
    def blocking():
        entered.set()
        finish.wait(5)
        assert owned.authority.agent_id == AID
        return original()
    app.seat.register = blocking
    app._release_owned_storage = lambda: LiteTUI._release_owned_storage(app)
    task = asyncio.create_task(LiteTUI._register_owned_startup(app))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    task.cancel()
    retry = asyncio.create_task(LiteTUI._register_owned_startup(app))
    shutdown = asyncio.create_task(LiteTUI._release_owned_storage(app))
    try:
        await asyncio.sleep(0.03)
        assert not task.done() and not shutdown.done() and calls == []
        assert owned.authority.agent_id == AID
    finally:
        finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await retry
    await shutdown
    assert calls == ['register', 'conversation-release']
    with pytest.raises(agent_ownership.OwnershipError):
        owned.authority


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['drain', 'store', 'early-shutdown'])
async def test_actual_teardown_exceptions_release_conversation_then_agent(owned, monkeypatch, failure):
    from litetui import hook_host, voice_backend, resource_session_lifecycle
    app, calls = registration_host(owned, succeeds=True)
    app._release_owned_storage = lambda: LiteTUI._release_owned_storage(app)
    app._stop_footer_sampler = lambda: None
    monkeypatch.setattr(voice_backend, 'stop', lambda: None)
    monkeypatch.setattr(hook_host, 'leave_conversation', lambda _: None)
    monkeypatch.setattr(hook_host, 'queue_lifecycle', lambda *_: None)
    async def drain(_):
        if failure == 'drain':
            raise RuntimeError('fixture lifecycle drain')
    monkeypatch.setattr(hook_host, 'drain_lifecycle', drain)
    def store_release():
        calls.append('conversation-release')
        assert owned.authority.agent_id == AID
        if failure == 'store':
            raise RuntimeError('fixture store release')
    app.store.release = store_release
    if failure == 'early-shutdown':
        def begin(_):
            raise RuntimeError('fixture early shutdown')
        monkeypatch.setattr(resource_session_lifecycle, 'begin_shutdown', begin)
        operation = LiteTUI._shutdown(app)
    else:
        operation = LiteTUI.on_unmount(app)
    with pytest.raises(RuntimeError):
        await operation
    assert calls == ['conversation-release']
    with agent_launch_context.acquire(owned.store.data_root, 'QuietHelm') as recovered:
        assert recovered.authority.agent_id == AID


def test_actual_cli_post_acquire_authority_failure_releases(root, monkeypatch):
    import sys
    from litetui import cli, paths, settings
    monkeypatch.setattr(paths, 'data_root', lambda: root)
    monkeypatch.setattr(settings, 'load', Settings)
    monkeypatch.setattr(sys, 'argv', ['litetui', '--agent', 'QuietHelm'])
    observed = []
    original = agent_launch_context.acquire
    class FailingAuthority:
        def __init__(self, session):
            self.session = session
        @property
        def authority(self):
            raise ValueError('fixture authority changed after acquisition')
        def release(self):
            observed.append('agent-release')
            self.session.release()
    monkeypatch.setattr(agent_launch_context, 'acquire', lambda *a, **k: FailingAuthority(original(*a, **k)))
    with pytest.raises(SystemExit):
        cli.main()
    assert observed == ['agent-release']
    with original(root, 'QuietHelm') as recovered:
        assert recovered.authority.agent_id == AID


@pytest.mark.asyncio
async def test_owned_interactive_model_not_loaded_refuses_before_provider(owned):
    from litetui.llm_backend import BackendError
    app, calls = registration_host(owned, succeeds=True)
    app._register_owned_startup = lambda: LiteTUI._register_owned_startup(app)
    app._rpc = False
    app.model_id = 'fixture'
    app._thinking_level = 'high'
    app.backend = SimpleNamespace(name='codex')
    app.model_rows = {'wrong': SimpleNamespace(key='wrong', loaded=True)}
    with pytest.raises(BackendError, match='no identity fallback'):
        await LiteTUI._ensure_chat_ready(app)
    assert calls == ['register']


def test_real_launch_options_cannot_supply_identity_overrides():
    from litetui.launch_options import LaunchOptions
    settings = Settings()
    assert LaunchOptions().overrides(settings, 'codex', 'fixture') == {}
    with pytest.raises(ValueError, match='Codex manages'):
        LaunchOptions(base_url='http://localhost:1234').overrides(settings, 'codex', 'fixture')
    values = LaunchOptions(base_url='http://localhost:1234', max_tokens=123).overrides(settings, 'lmstudio', 'fixture')
    assert not {'backend', 'default_model', 'thinking_level'}.intersection(values)


@pytest.mark.asyncio
async def test_repeated_cancel_shutdown_joins_one_cleanup_after_registration(owned):
    import threading
    entered, finish = threading.Event(), threading.Event()
    app, calls = registration_host(owned, succeeds=True)
    original = app.seat.register
    def blocking():
        entered.set()
        finish.wait(5)
        assert owned.authority.agent_id == AID
        return original()
    app.seat.register = blocking
    registration = asyncio.create_task(LiteTUI._register_owned_startup(app))
    assert await asyncio.to_thread(entered.wait, 2)
    cleanup = asyncio.create_task(LiteTUI._release_owned_storage(app))
    await asyncio.sleep(0)
    cleanup.cancel()
    await asyncio.sleep(0)
    cleanup.cancel()
    second_cleanup = asyncio.create_task(LiteTUI._release_owned_storage(app))
    try:
        await asyncio.sleep(0.03)
        assert not cleanup.done() and not second_cleanup.done()
        assert owned.authority.agent_id == AID and calls == []
    finally:
        finish.set()
    assert await registration
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    await second_cleanup
    await LiteTUI._release_owned_storage(app)
    assert calls == ['register', 'conversation-release']
    with agent_launch_context.acquire(owned.store.data_root, 'QuietHelm') as recovered:
        assert recovered.authority.agent_id == AID


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['backend', 'effort-override', 'thinking', 'model'])
async def test_actual_owned_provider_readiness_rejects_effective_authority_change(owned, change):
    from litetui.llm_backend import BackendError
    app, calls = registration_host(owned, succeeds=True)
    provider_calls = []
    async def ask(model):
        provider_calls.append(model)
    app._register_owned_startup = lambda: LiteTUI._register_owned_startup(app)
    app._rpc = False
    app.model_id = 'fixture'
    app._thinking_level = 'high'
    app.backend = SimpleNamespace(name='codex', ensure_chat_ready=ask)
    app._effective_request_overrides = lambda: {'reasoning_effort': 'low'} if change == 'effort-override' else {}
    if change == 'backend':
        app.backend.name = 'claude'
    elif change == 'thinking':
        app._thinking_level = 'low'
    elif change == 'model':
        app.model_id = 'wrong'
    with pytest.raises(BackendError, match='authority'):
        await LiteTUI._ensure_chat_ready(app)
    assert provider_calls == []
    assert calls == ['register']


@pytest.mark.asyncio
async def test_global_task_sweep_waits_for_actual_registration_thread_and_cleanup(owned):
    import threading
    entered, finish = threading.Event(), threading.Event()
    app, calls = registration_host(owned, succeeds=True)
    original = app.seat.register
    def blocking():
        entered.set()
        finish.wait(5)
        assert owned.authority.agent_id == AID
        return original()
    app.seat.register = blocking
    registration = asyncio.create_task(LiteTUI._register_owned_startup(app))
    assert await asyncio.to_thread(entered.wait, 2)
    cleanup = asyncio.create_task(LiteTUI._release_owned_storage(app))
    await asyncio.sleep(0)
    async def finish_later():
        await asyncio.sleep(0.03)
        assert owned.authority.agent_id == AID and calls == []
        assert app._owned_registration_lock.locked()
        finish.set()
    driver = asyncio.current_task()
    victims = [task for task in asyncio.all_tasks() if task is not driver]
    for task in victims:
        task.cancel()
    # This controlled signal is created AFTER the simulated asyncio.run sweep.
    finisher = asyncio.create_task(finish_later())
    try:
        await asyncio.wait_for(asyncio.gather(*victims, return_exceptions=True), 3)
        await finisher
    finally:
        finish.set()
    assert registration.cancelled()
    assert cleanup.cancelled()
    assert calls == ['register', 'conversation-release']
    with agent_launch_context.acquire(owned.store.data_root, 'QuietHelm') as recovered:
        assert recovered.authority.agent_id == AID


@pytest.mark.asyncio
async def test_real_effective_overrides_and_turnengine_effort_rejected_before_provider(owned):
    from litetui.llm_backend import BackendError
    from litetui.turn_engine import TurnEngine
    app, calls = registration_host(owned, succeeds=True)
    provider_calls = []
    async def ask(model):
        provider_calls.append(model)
    app._register_owned_startup = lambda: LiteTUI._register_owned_startup(app)
    app._rpc = False
    app.model_id = 'fixture'
    app._thinking_level = 'high'
    app.backend = SimpleNamespace(name='codex', ensure_chat_ready=ask,
        request_overrides=lambda model: {'reasoning_effort': 'low'})
    app._effective_request_overrides = lambda: LiteTUI._effective_request_overrides(app)
    kwargs = TurnEngine.chat_request(model_id=app.model_id, messages=[], tools_enabled=False,
        max_tokens_tools=100, max_tokens_chat=100,
        request_overrides=app._effective_request_overrides(), thinking_level=app._thinking_level,
        backend_name='codex')
    assert kwargs['extra_body']['reasoning_effort'] == 'low'
    with pytest.raises(BackendError, match='Effective request effort'):
        await LiteTUI._ensure_chat_ready(app)
    assert provider_calls == [] and calls == ['register']


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['stream-claude', 'compact-claude', 'compact-codex'])
@pytest.mark.parametrize('change', ['backend', 'effort'])
async def test_actual_native_entry_refuses_authority_before_provider(owned, monkeypatch, route, change):
    from litetui import claude_turn, claude_compact, model_transport
    calls, said = [], []
    backend_name = 'claude' if route.endswith('claude') else 'codex'
    authority = SimpleNamespace(backend=backend_name, model='fixture', thinking_level='high')
    # A shim only changes backend authority to exercise Claude's actual mapper;
    # real owned lease remains held to prove no user/provider/registry mutation.
    app = SimpleNamespace(_agent_session=SimpleNamespace(authority=authority), model_id='fixture',
        _thinking_level='high', thinking_level='high', _system=said.append,
        settings=SimpleNamespace(model_infer_overrides={}, lmstudio_graded_thinking_models=()),
        backend=SimpleNamespace(name=backend_name, owns_native_turns=backend_name == 'claude',
                               reasoning_levels=lambda _: ['low', 'high']))
    app._effective_request_overrides = lambda: {'reasoning_effort': 'low'} if change == 'effort' else {}
    async def maintenance():
        pass
    app._await_mcp_maintenance = maintenance
    async def provider(*a, **k):
        calls.append('provider')
    monkeypatch.setattr(claude_turn, 'stream_turn', provider)
    monkeypatch.setattr(claude_compact, 'compact', provider)
    monkeypatch.setattr(model_transport, 'for_app', lambda *_: SimpleNamespace(compact=provider))
    if route == 'compact-codex':
        app.backend.app_server = object()
    if change == 'backend':
        app.backend.name = 'wrong-backend'
    elif backend_name == 'claude':
        app.settings.model_infer_overrides = {'fixture': {'reasoning_effort': 'low'}}
    operation = LiteTUI._stream.__wrapped__(app) if route.startswith('stream') else LiteTUI._compact.__wrapped__(app)
    await operation
    assert calls == []
    assert len(said) == 1 and 'authority' in said[0]
    assert owned.authority.agent_id == AID
