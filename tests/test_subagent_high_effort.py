"""Explicit child model + reasoning effort survives schema, runner, and Codex wire."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from litetui import model_transport as mt
from litetui import tool_schemas
from litetui.plugins.subagent_plugin import _make_runner

SOL = "gpt-5.6-sol"


def _remote_app(levels=("low", "medium", "high")):
    metadata = {
        SOL: {
            "supported_reasoning_levels": [
                {"effort": effort} for effort in levels
            ]
        }
    }
    backend = SimpleNamespace(
        remote=True,
        name="codex",
        models=metadata,
        reasoning_levels=lambda model: [
            row["effort"]
            for row in metadata.get(model, {}).get("supported_reasoning_levels", [])
        ],
    )
    return SimpleNamespace(
        backend=backend,
        model_id="gpt-parent",
        model_rows={},
        settings=SimpleNamespace(subagent_model=None, compact_max_tokens=12288),
    )


def test_schema_exposes_explicit_reasoning_effort_and_sol_high_example() -> None:
    spec = tool_schemas.load("subagent")["function"]
    props = spec["parameters"]["properties"]

    assert "reasoning_effort" in props
    assert "high" in props["reasoning_effort"]["enum"]
    description = spec["description"] + props["model"]["description"]
    assert SOL in description
    assert "reasoning_effort" in description and "high" in description.lower()


def test_runner_passes_explicit_sol_and_high_without_changing_parent() -> None:
    app = _remote_app()
    captured = {}

    def complete(_app, payload, **_kwargs):
        captured.update(payload)
        return {"choices": [{"message": {"content": "ok"}}]}

    with patch(
        "litetui.plugins.subagent_plugin.model_transport.complete_sidecall",
        complete,
    ):
        result = _make_runner(app)(
            {
                "prompt": "review this",
                "model": SOL,
                "reasoning_effort": "high",
            }
        )

    assert "[error]" not in result
    assert captured["model"] == SOL
    assert captured["reasoning_effort"] == "high"
    assert app.model_id == "gpt-parent"


def test_remote_transport_puts_explicit_high_on_the_codex_wire(monkeypatch) -> None:
    app = _remote_app()
    captured = {}

    async def create(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="answer", reasoning_content="")
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(mt.OAuthTransport, "create", create)
    result = mt.complete_sidecall(
        app,
        {
            "model": SOL,
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
        },
    )

    assert result["choices"][0]["message"]["content"] == "answer"
    assert captured["model"] == SOL
    assert captured["extra_body"] == {"reasoning_effort": "high"}


def test_remote_transport_refuses_unsupported_effort_without_fallback(monkeypatch) -> None:
    app = _remote_app(levels=("low", "medium"))
    called = False

    async def create(self, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(mt.OAuthTransport, "create", create)
    with pytest.raises(mt.ProviderError, match="does not support.*high"):
        mt.complete_sidecall(
            app,
            {
                "model": SOL,
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": "high",
            },
        )

    assert called is False


def test_legacy_none_still_maps_to_models_minimum(monkeypatch) -> None:
    app = _remote_app(levels=("low", "medium", "high"))
    captured = {}

    async def create(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="answer", reasoning_content="")
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(mt.OAuthTransport, "create", create)
    mt.complete_sidecall(
        app,
        {
            "model": SOL,
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "none",
        },
    )

    assert captured["extra_body"] == {"reasoning_effort": "low"}


def test_legacy_think_true_omission_still_maps_to_medium(monkeypatch) -> None:
    app = _remote_app(levels=("low", "medium", "high"))
    captured = {}

    async def create(self, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="answer", reasoning_content="")
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(mt.OAuthTransport, "create", create)
    mt.complete_sidecall(
        app,
        {
            "model": SOL,
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert captured["extra_body"] == {"reasoning_effort": "medium"}
