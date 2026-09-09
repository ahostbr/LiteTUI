"""T537 — --model flag picks a loaded model; fails loudly on unloaded/missing."""
import asyncio
from types import SimpleNamespace

from litetui.app import LiteTUI
from litetui.llm_backend import ModelRow


class _FakeApp(SimpleNamespace):
    """Minimal double for the fields _apply_cli_args reads and writes."""

    def __init__(self, model_id, cli_model, rows):
        super().__init__(
            model_id=model_id,
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

    def _update_header(self):
        self._headers += 1

    def _fetch_ctx_window(self):
        self._ctx_fetched += 1

    def _system(self, msg):
        self._said.append(msg)


_apply = LiteTUI._apply_cli_args.__wrapped__


def _run(app):
    asyncio.get_event_loop().run_until_complete(_apply(app))


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
