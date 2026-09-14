from types import SimpleNamespace as NS

import pytest

from litetui.thinking_capabilities import set_thinking, thinking_capabilities
from litetui.turn_engine import _resolve_reasoning_effort


def app(name="codex", levels=None):
    backend = NS(name=name)
    if name == "codex":
        backend.reasoning_levels = lambda model: levels or ["low", "medium", "high"]
    return NS(backend=backend, model_id="test", thinking_level="medium",
              settings=NS(model_infer_overrides={"test": {"reasoning_effort": "high"}},
                          lmstudio_graded_thinking_models=[]))


def test_codex_metadata_and_invalid_level():
    a = app(levels=["low", "high", "ultra"])
    assert thinking_capabilities(a)["levels"] == ["default", "low", "high", "ultra"]
    with pytest.raises(ValueError):
        set_thinking(a, "off")
    assert a.thinking_level == "medium"
    set_thinking(a, "ultra")
    assert a.settings.model_infer_overrides["test"]["reasoning_effort"] == "ultra"


def test_default_clears_override_off_is_not_default():
    a = app(levels=["none", "low"])
    set_thinking(a, "off")
    assert a.thinking_level == "off"
    assert _resolve_reasoning_effort(a.thinking_level, "codex") == "none"
    set_thinking(a, "default")
    assert a.thinking_level is None
    assert "reasoning_effort" not in a.settings.model_infer_overrides["test"]


def test_local_adapter_does_not_advertise_ignored_graded_values():
    a = app("lmstudio")
    a._model_thinking_levels = ["off", "low", "medium", "xhigh"]
    assert thinking_capabilities(a)["levels"] == ["default", "off"]
    a.settings.lmstudio_graded_thinking_models = ["test"]
    assert thinking_capabilities(a)["levels"] == ["default", "off", "low", "medium", "xhigh"]


def test_unknown_backend_support_is_not_guessed():
    assert thinking_capabilities(app("unknown"))["levels"] == ["default"]


def test_rpc_reports_capabilities_and_rejects_invalid_choice(monkeypatch):
    from litetui import rpc
    responses = []
    monkeypatch.setattr(rpc, "_respond", lambda id, **body: responses.append(body))
    a = app(levels=["none", "low"])
    rpc._dispatch(a, {"id": "1", "type": "get_settings"})
    assert responses[-1]["result"]["thinking_capabilities"]["levels"] == ["default", "off", "low"]
    rpc._dispatch(a, {"id": "2", "type": "set_settings", "patch": {"thinking_level": "off"}})
    assert responses[-1]["result"]["thinking_level"] == "off"
    rpc._dispatch(a, {"id": "3", "type": "set_settings", "patch": {"thinking_level": "ultra"}})
    assert responses[-1]["ok"] is False
    assert a.thinking_level == "off"


def test_llamacpp_exposes_adapter_vocabulary_with_model_caveat():
    from litetui.settings import THINKING_LEVELS
    capability = thinking_capabilities(app("llamacpp"))
    assert capability["levels"] == ["default", *THINKING_LEVELS]
    assert "chat template" in capability["note"]


@pytest.mark.parametrize("choice,restored", [("off", "off"), ("default", None)])
def test_choice_survives_conversation_reopen(tmp_path, choice, restored):
    from test_convo_settings import _App, _dir

    from litetui import settings as st
    a = _App(st.Settings(), _dir(tmp_path), backend_name="llamacpp")
    a._adopt_convo_settings(born=True)
    set_thinking(a, choice)
    a._adopt_convo_settings(born=False)
    assert a.thinking_level == restored
