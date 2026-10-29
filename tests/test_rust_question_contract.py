"""Real Python question serialization/answer semantics shared with Rust; no provider."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import ask_user_question as auq
from litetui import rpc

FIXTURE = Path(__file__).parents[1] / "rust/litetui-terminal/tests/fixtures/question_request.json"


@pytest.mark.parametrize("action", ["submit", "chat", "cancel"])
def test_rust_question_fixture_and_answer_use_real_source_contract(monkeypatch, action):
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    events = []
    replies = []
    monkeypatch.setattr(auq, "uuid4", lambda: SimpleNamespace(hex="0" * 32))
    monkeypatch.setattr(rpc, "rpc_emit", replies.append)
    app = SimpleNamespace(is_running=True, _rpc=True)

    def emit(event):
        events.append(event)
        if event["type"] == "user_input_requested":
            rpc._dispatch(app, {
                "type": "answer", "id": "rust-question-answer-0",
                "ask_id": event["id"], "action": action,
                "answers": [] if action == "cancel" else [
                    {"selected": [0, 1], "note": "both"},
                    {"selected": [], "note": "fixture-secret"},
                ],
            })

    app._rpc_emit = emit
    states = auq._parse_questions({"questions": [
        {"label": "Scope", "question": "Which files?", "options": [
            {"title": "Source", "description": "Implementation"},
            {"title": "Tests", "description": "Regression coverage"},
        ]},
        {"label": "Token", "question": "Enter a fixture token", "options": [],
         "allowFreeText": True, "multiSelect": False, "isSecret": True},
    ]})
    result = auq._run_over_rpc(states, app)
    assert {key: events[0][key] for key in expected} == expected
    assert replies == [{
        "type": "response", "id": "rust-question-answer-0", "ok": True,
        "result": {"answered": expected["id"], "id_key": "ask_id"},
    }]
    assert app._rpc_pending_asks == {}
    if action == "cancel":
        assert "CANCELLED" in result and "fixture-secret" not in result
    else:
        assert ("SUBMITTED" if action == "submit" else "CHAT ABOUT THIS") in result
        assert "[x] Source" in result and "[x] Tests" in result
        assert "fixture-secret" in result  # Returned only to the owning runtime/model.
