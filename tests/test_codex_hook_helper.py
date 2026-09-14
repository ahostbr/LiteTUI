import io
import json
from types import SimpleNamespace as NS

import pytest

from litetui import codex_hook_helper as helper


@pytest.mark.parametrize(
    "payload", [b'{"hook_event_name":"PreToolUse"}', b"[]", b"not json"]
)
def test_missing_policy_or_invalid_input_emits_supported_pretool_denial(
    monkeypatch, capsys, payload
):
    monkeypatch.delenv("LITETUI_CODEX_HOOK_KEY", raising=False)
    monkeypatch.setattr(helper.sys, "stdin", NS(buffer=io.BytesIO(payload)))
    assert helper.main() == 0
    output = capsys.readouterr()
    assert not output.err
    response = json.loads(output.out)
    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "continue" not in response


def test_posttool_failure_uses_posttool_block_feedback(monkeypatch, capsys):
    monkeypatch.delenv("LITETUI_CODEX_HOOK_KEY", raising=False)
    monkeypatch.setattr(
        helper.sys, "stdin", NS(buffer=io.BytesIO(b'{"hook_event_name":"PostToolUse"}'))
    )
    assert helper.main() == 0
    response = json.loads(capsys.readouterr().out)
    assert response == {
        "decision": "block",
        "reason": "LiteTUI native tool policy is unavailable.",
    }
