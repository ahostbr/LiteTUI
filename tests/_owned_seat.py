"""Real owned temporary seats, with explicit unrelated external-effect controls.

The constructor, store and session are real. Default fixture observers do not
probe thinking, discover live LM Studio models/residency, or start an updater
thread. A test measuring one of those doors opts out with ``real_effects`` and
supplies its controlled seam before invoking the real subject. Model-info and
apply/load operations are NOT controlled here: their owning tests control them.

Controls start before construction and remain until release_all, after pilot
teardown. All owned apps in one test share that policy; conflicting policies
fail rather than silently changing another app's measured subject.
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

_OPEN: list = []
_APPS: list = []
_OBSERVER_APPS: list = []
_EFFECTS: ExitStack | None = None
_REAL_EFFECTS: frozenset | None = None
_EFFECT_NAMES = {"thinking", "model_list", "readiness", "updates"}


def _fixture_rows(backend):
    from litetui.llm_backend import ModelRow

    # Fixture catalogs describe availability, not implicit resident weights.
    for app in reversed(_OBSERVER_APPS):
        if app.backend is backend:
            rows = getattr(app, "model_rows", {})
            return [rows.get(key) or ModelRow(key=key, path=None, source="fixture", loaded=False)
                    for key in app.available_models]
    return None  # Unrelated backend units retain their real implementation.


def control_external_effects(app=None, *, real_effects=()):
    """Hold unrelated observer controls; opted-out subjects must supply a seam."""
    global _EFFECTS, _REAL_EFFECTS
    real = frozenset(real_effects)
    if real - _EFFECT_NAMES:
        raise ValueError(f"Unknown external effects: {sorted(real - _EFFECT_NAMES)}")
    if _EFFECTS is not None:
        if real != _REAL_EFFECTS:
            raise ValueError("Owned apps in one runtime must use the same external-effect policy")
        if app is not None and not any(owner is app for owner in _OBSERVER_APPS):
            _OBSERVER_APPS.append(app)
        return
    from litetui import llm_backend, thinking_probe, update_check

    stack = ExitStack()
    try:
        if "thinking" not in real:
            stack.enter_context(patch.object(thinking_probe, "get_effective_levels", lambda *a, **k: None))
        if "model_list" not in real:
            original_listing = llm_backend.LMStudioBackend.list_models
            async def listing(backend):
                rows = _fixture_rows(backend)
                return await original_listing(backend) if rows is None else rows
            stack.enter_context(patch.object(llm_backend.LMStudioBackend, "list_models", listing))
        if "readiness" not in real:
            original_loaded = llm_backend.LMStudioBackend.loaded_models
            def loaded(backend):
                rows = _fixture_rows(backend)
                return original_loaded(backend) if rows is None else [row.key for row in rows if row.loaded]
            stack.enter_context(patch.object(llm_backend.LMStudioBackend, "loaded_models", loaded))
        if "updates" not in real:
            # Do not birth a daemon which reads mutable environment/ROOT later.
            original_update = update_check.start_background_check
            def start_update(app):
                if not any(owner is app for owner in _OBSERVER_APPS):
                    original_update(app)
            stack.enter_context(patch.object(update_check, "start_background_check", start_update))
    except BaseException:
        stack.close()
        raise
    _EFFECTS, _REAL_EFFECTS = stack, real
    if app is not None:
        _OBSERVER_APPS.append(app)


def owned_app(*args, real_effects=(), **kwargs):
    """Construct a real owned app; real_effects opts observer subjects out."""
    control_external_effects(real_effects=real_effects)
    from litetui import app as app_mod
    from litetui import paths, settings
    from litetui.agent_launch_context import ordinary

    # Like cli.py: a spawned launch's identity opens the owned home, then rides to the app.
    session = ordinary(paths.data_root(), settings.load(), notice=lambda _text: None,
                       spawned_identity=kwargs.get("spawn_identity"),
                       backend=kwargs.get("initial_backend"), model=kwargs.get("initial_model"))
    _OPEN.append(session)
    app = app_mod.LiteTUI(*args, agent_session=session, **kwargs)
    _APPS.append(app)
    _OBSERVER_APPS.append(app)
    return app


def release_all() -> None:
    global _EFFECTS, _REAL_EFFECTS
    try:
        try:
            while _APPS:
                app = _APPS.pop()
                store = getattr(app, "store", None)
                if store is not None:
                    store.release()
        finally:
            while _OPEN:
                _OPEN.pop().release()
    finally:
        stack, _EFFECTS, _REAL_EFFECTS = _EFFECTS, None, None
        try:
            if stack is not None:
                stack.close()
        finally:
            _OBSERVER_APPS.clear()
