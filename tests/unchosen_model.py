"""The state /backend leaves a seat in until /model: the backend kept, no model chosen.

Ryan 2026-11-12: "/backend switches backend only /model displays models that backend provides".
App.apply_backend_change publishes the agent's execution with model None (model_selection
"unchosen") and sets model_id to "". Python's app keeps working in that state; only a turn
refuses ("No model chosen for ...").
"""
from __future__ import annotations


def unchoose(app):
    authority = app._agent_session.authority
    app._agent_session.update_execution(backend=authority.backend, model=None,
                                        thinking_level=authority.thinking_level)
    app.model_id = ""
    app._cli_initial_model = app._user_model_choice = None  # apply_backend_change clears both
    assert app._agent_session.authority.model is None
    return app
