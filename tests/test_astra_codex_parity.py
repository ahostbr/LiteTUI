from types import SimpleNamespace as NS

import pytest

from litetui.model_transport import codex_request, for_app
from litetui.plugins.misc import _cmd_think, _thinking_rows

LEVELS = ["low", "medium", "high", "xhigh", "max", "ultra"]


@pytest.mark.parametrize("effort", LEVELS)
def test_command_to_wire_preserves_each_astra_effort(effort):
    app = NS(
        backend=NS(name="codex", reasoning_levels=lambda model: LEVELS),
        model_id="gpt-6-astra",
        thinking_level="medium",
        settings=NS(
            model_infer_overrides={"gpt-6-astra": {"reasoning_effort": "high"}}
        ),
        update_header=lambda: None,
        system_message=lambda message: None,
    )
    assert [value for value, _ in _thinking_rows(app)] == [*LEVELS, "unset"]
    _cmd_think(app, "think", effort)
    override = app.settings.model_infer_overrides[app.model_id]
    body = codex_request(
        {"model": app.model_id, "messages": [], "extra_body": override}
    )
    assert app.thinking_level == effort
    assert body["reasoning"]["effort"] == effort
    _cmd_think(app, "think", "off")
    assert app.thinking_level == effort


def test_cache_key_survives_transport_recreation_and_tools_stay_stable():
    app = NS(backend=NS(name="codex", models={}), convo_id="conversation-a")
    assert (
        for_app(app).prompt_cache_key
        == for_app(app).prompt_cache_key
        == "conversation-a"
    )
    app.convo_id = "conversation-b"
    assert for_app(app).prompt_cache_key == "conversation-b"
    request = {
        "model": "gpt-6-astra",
        "messages": [{"role": "user", "content": "Hello"}],
        "tools": [{"type": "function", "function": {"name": "read", "parameters": {}}}],
        "prompt_cache_key": "conversation-b",
    }
    enabled = codex_request(request)
    disabled = codex_request({**request, "tool_choice": "none"})
    assert enabled["tools"] == disabled["tools"]
    assert enabled["input"] == disabled["input"]
    assert disabled["tool_choice"] == "none"
    assert disabled["prompt_cache_key"] == "conversation-b"
