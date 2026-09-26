"""T537 — --model flag picks a loaded model; fails loudly on unloaded/missing."""
import asyncio
from types import SimpleNamespace

from litetui.app import LiteTUI
from litetui.llm_backend import ModelRow


class _FakeApp(SimpleNamespace):
    """Minimal double for the fields _apply_cli_args reads and writes."""

    def __init__(self, model_id, cli_model, rows):
        super().__init__(
            _model_id=model_id,
            _cli_initial_model=cli_model,
            _cli_system_prompt=None,
            _first_prompt=None,
            available_models=[r.key for r in rows],
            model_rows={r.key: r for r in rows},
            _said=[],
            _headers=0,
            _ctx_fetched=0,
            conversation=[],
        )

    @property
    def model_id(self):
        return self._model_id

    def _update_header(self):
        self._headers += 1

    def _fetch_ctx_window(self):
        self._ctx_fetched += 1

    def _system(self, msg):
        self._said.append(msg)

    def _resume_cli_convo(self):
        """This unit double has no --convo; the production preflight succeeds."""
        return True


_apply = LiteTUI._apply_cli_args.__wrapped__


def _run(app):
    # 🔴 `asyncio.get_event_loop()` RAISES HERE ON PYTHON 3.12+ (T579).
    # It stopped creating a loop implicitly when none is running, so all
    # five arms in this file died with
    #     RuntimeError: There is no current event loop in thread 'MainThread'
    # before touching a line of the code under test. This is not an
    # artefact of one worktree's interpreter: the call is wrong on every
    # modern Python, and it would fail the same way in the primary clone
    # the day that one moves off 3.11.
    #
    # `asyncio.run` is this suite's own idiom (ten other call sites) and
    # it also CLOSES the loop, which the old spelling never did.
    asyncio.run(_apply(app))


def test_flag_picks_loaded_model():
    rows = [
        ModelRow(key="big-27b", path=None, source="server", loaded=True),
        ModelRow(key="small-2b", path=None, source="server", loaded=True),
    ]
    app = _FakeApp("big-27b", "small-2b", rows)
    _run(app)
    assert app.model_id == "small-2b"
    assert app._headers == 1
    assert app._ctx_fetched == 1


def test_flag_updates_header():
    rows = [
        ModelRow(key="alpha", path=None, source="server", loaded=True),
        ModelRow(key="beta", path=None, source="server", loaded=True),
    ]
    app = _FakeApp("alpha", "beta", rows)
    _run(app)
    assert app._headers >= 1


def test_unloaded_model_fails_loudly():
    rows = [
        ModelRow(key="big-27b", path=None, source="server", loaded=True),
        ModelRow(key="cold-7b", path=None, source="server", loaded=False),
    ]
    app = _FakeApp("big-27b", "cold-7b", rows)
    _run(app)
    assert app.model_id == "big-27b"  # unchanged
    assert any("NOT loaded" in s for s in app._said)


def test_missing_model_fails_loudly():
    rows = [
        ModelRow(key="big-27b", path=None, source="server", loaded=True),
    ]
    app = _FakeApp("big-27b", "ghost-99b", rows)
    _run(app)
    assert app.model_id == "big-27b"  # unchanged
    assert any("not found" in s for s in app._said)


def test_no_flag_does_nothing():
    rows = [
        ModelRow(key="big-27b", path=None, source="server", loaded=True),
    ]
    app = _FakeApp("big-27b", None, rows)
    _run(app)
    assert app.model_id == "big-27b"
    assert app._headers == 0


def test_missing_explicit_model_never_submits_prompt_to_previous_model():
    app = _FakeApp('previous', 'missing', [ModelRow(key='previous', path=None, source='server', loaded=True)])
    app._first_prompt = 'make a change'
    submitted = []
    async def ready(**kwargs):
        return True
    app._ensure_chat_ready = ready
    app._submit_text = lambda *args, **kwargs: submitted.append(args)
    _run(app)
    assert not submitted
    assert app._cli_launch_error


def test_unloaded_explicit_model_never_submits_prompt_to_previous_model():
    app = _FakeApp('previous', 'cold', [ModelRow(key='cold', path=None, source='server', loaded=False)])
    app._first_prompt = 'make a change'
    submitted = []
    async def ready(**kwargs):
        return True
    app._ensure_chat_ready = ready
    app._submit_text = lambda *args, **kwargs: submitted.append(args)
    _run(app)
    assert not submitted
    assert app._cli_launch_error


def test_headless_explicit_missing_model_refuses_resident_substitution():
    app = SimpleNamespace(model_id='resident', _cli_initial_model='missing',
                          backend=SimpleNamespace(loaded_models=lambda: ['resident']))
    action, model, reason = LiteTUI._headless_model_decision(app)
    assert action == 'refuse'
    assert model is None
    assert 'missing' in reason


def test_headless_explicit_resident_model_wins_before_cli_worker():
    app = SimpleNamespace(model_id='previous', _cli_initial_model='requested',
                          backend=SimpleNamespace(loaded_models=lambda: ['previous', 'requested']))
    action, model, reason = LiteTUI._headless_model_decision(app)
    assert model == 'requested'
    assert action in ('ok', 'substitute')


def test_cli_thinking_is_effective_only_and_validated_before_prompt():
    app = _FakeApp('model', None, [ModelRow(key='model', path=None, source='server', loaded=True)])
    app._cli_thinking_level = 'high'
    app.backend = SimpleNamespace(name='codex', reasoning_levels=lambda model: ['low', 'high'])
    app.settings = SimpleNamespace(model_infer_overrides={'model': {'reasoning_effort': 'low'}})
    app._thinking_level = 'low'
    _run(app)
    assert app._thinking_level == 'high'
    assert app._cli_effective_thinking == 'high'
    assert app.settings.model_infer_overrides['model']['reasoning_effort'] == 'low'


def test_unsupported_cli_thinking_blocks_prompt():
    app = _FakeApp('model', None, [ModelRow(key='model', path=None, source='server', loaded=True)])
    app._cli_thinking_level = 'unsupported'
    app.backend = SimpleNamespace(name='codex', reasoning_levels=lambda model: ['low'])
    app._first_prompt = 'must not run'
    submitted = []
    async def ready(**kwargs):
        return True
    app._ensure_chat_ready = ready
    app._submit_text = lambda *args, **kwargs: submitted.append(args)
    _run(app)
    assert not submitted
    assert app._cli_launch_error


def test_invocation_effort_overrides_request_copy_not_backend_preferences():
    original = {'reasoning_effort': 'low', 'temperature': .2}
    app = SimpleNamespace(backend=SimpleNamespace(name='codex', reasoning_levels=lambda model: ['low', 'high'], request_overrides=lambda model: original),
                          model_id='model', _cli_effective_thinking='high')
    assert LiteTUI._effective_request_overrides(app)['reasoning_effort'] == 'high'
    assert original['reasoning_effort'] == 'low'
    app._cli_effective_thinking = 'default'
    assert 'reasoning_effort' not in LiteTUI._effective_request_overrides(app)
    assert original['reasoning_effort'] == 'low'


def test_deliberate_thinking_choice_retires_cli_override():
    recorded = []
    app = SimpleNamespace(_cli_effective_thinking='high', _cli_thinking_level='high',
                          _backend=SimpleNamespace(name='codex'),
                          _remember_for_this_convo=lambda key, value: recorded.append((key, value)))
    LiteTUI.thinking_level.fset(app, 'low')
    assert app._cli_effective_thinking is None
    assert app._cli_thinking_level is None
    assert ('reasoning_effort', 'low') in recorded


def test_cli_reasoning_reaches_serialized_turn_request_without_persistence():
    import json
    from litetui.turn_engine import TurnEngine
    original = {'reasoning_effort': 'low'}
    app = SimpleNamespace(backend=SimpleNamespace(name='codex', reasoning_levels=lambda model: ['low', 'high'], request_overrides=lambda model: original),
                          model_id='model', _cli_effective_thinking='high')
    wire = TurnEngine.chat_request(model_id='model', messages=[{'role': 'user', 'content': 'hi'}],
        tools_enabled=False, max_tokens_tools=100, max_tokens_chat=100,
        request_overrides=LiteTUI._effective_request_overrides(app), thinking_level='low',
        backend_name='codex')
    assert json.loads(json.dumps(wire))['extra_body']['reasoning_effort'] == 'high'
    assert original == {'reasoning_effort': 'low'}


def test_resumed_or_changed_model_revalidates_cli_effort_before_request():
    from litetui.llm_backend import BackendError
    import pytest
    app = SimpleNamespace(backend=SimpleNamespace(name='codex',
        request_overrides=lambda model: {}, reasoning_levels=lambda model: ['low']),
        model_id='changed-model', _cli_effective_thinking='high')
    with pytest.raises(BackendError, match='thinking'):
        LiteTUI._effective_request_overrides(app)


def test_cli_startup_failure_sets_blocked_before_releasing_ready_waiter():
    import pytest
    app = _FakeApp('model', None, [ModelRow(key='model', path=None, source='server', loaded=True)])
    app._first_prompt = 'hello'
    async def failed(**kwargs):
        raise RuntimeError('readiness fixture')
    app._ensure_chat_ready = failed
    async def scenario():
        app._cli_args_done = asyncio.Event()
        with pytest.raises(RuntimeError, match='readiness fixture'):
            await _apply(app)
        assert app._cli_args_done.is_set()
        assert 'readiness fixture' in app._cli_launch_error
    asyncio.run(scenario())


def test_cli_startup_cancellation_sets_blocked_and_preserves_cancellation():
    import pytest
    app = _FakeApp('model', None, [ModelRow(key='model', path=None, source='server', loaded=True)])
    app._first_prompt = 'hello'
    async def scenario():
        entered = asyncio.Event()
        app._cli_args_done = asyncio.Event()
        async def wait(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        app._ensure_chat_ready = wait
        task = asyncio.create_task(_apply(app))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app._cli_args_done.is_set()
        assert 'cancelled' in app._cli_launch_error.lower()
    asyncio.run(scenario())
