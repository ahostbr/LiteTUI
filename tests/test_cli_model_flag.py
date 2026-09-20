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
