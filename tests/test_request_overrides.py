"""Per-request sampling: global /settings defaults, per-model overrides on
top, and the native-vs-extra_body split.

The split is not cosmetic: openai's create() has typed params and NO
**kwargs, so top_k/min_p/repeat_penalty as top-level keys are a TypeError.
(The pre-seam call site updated kwargs with sampling_kwargs() directly and
would have crashed the first time anyone set top_k — verified against
openai 2.26.0.)
"""
from __future__ import annotations

import pytest

from llm_backend import (
    BackendError,
    LMStudioBackend,
    OPENAI_NATIVE_PARAMS,
    split_request_kwargs,
)
from settings import Settings


def _backend(**infer) -> LMStudioBackend:
    s = Settings()
    s.temperature = 0.7
    s.top_k = 40
    if infer:
        s.model_infer_overrides = infer
    return LMStudioBackend(s)


def test_global_only():
    got = _backend().request_overrides("m")
    assert got["temperature"] == 0.7 and got["top_k"] == 40


def test_override_wins():
    got = _backend(m={"temperature": 1.0}).request_overrides("m")
    assert got["temperature"] == 1.0
    assert got["top_k"] == 40          # untouched global survives


def test_other_models_inherit():
    got = _backend(m={"temperature": 1.0}).request_overrides("other")
    assert got["temperature"] == 0.7


def test_explicit_none_forces_server_default():
    got = _backend(m={"temperature": None}).request_overrides("m")
    assert "temperature" not in got, "None must OMIT, not send null"


def test_split_native_vs_extra():
    native, extra, rf = split_request_kwargs(
        {"temperature": 1.0, "top_k": 20, "min_p": 0.05, "stop": ["x"]}
    )
    assert native == {"temperature": 1.0, "stop": ["x"]}
    assert extra == {"top_k": 20, "min_p": 0.05}
    assert rf is None
    assert "top_k" not in OPENAI_NATIVE_PARAMS   # the premise the split rests on


def test_json_schema_becomes_response_format():
    native, extra, rf = split_request_kwargs({"json_schema": '{"type": "object"}'})
    assert rf == {
        "type": "json_schema",
        "json_schema": {"name": "litetui_schema", "schema": {"type": "object"}},
    }
    assert native == {} and extra == {}


def test_invalid_schema_is_refused_loudly():
    with pytest.raises(BackendError):
        split_request_kwargs({"json_schema": "{not json"})


def test_enable_thinking_rides_chat_template_kwargs():
    _native, extra, _rf = split_request_kwargs({"enable_thinking": False})
    assert extra == {"chat_template_kwargs": {"enable_thinking": False}}
