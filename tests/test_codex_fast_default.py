"""Codex defaults to standard routing by omitting ``service_tier``.

This follows the installed Codex 0.153.3 contract at upstream commit
b1a547b1f73ce86205d9222ac19cff334b3b7a2e:

* ``protocol/src/config_types.rs`` lines 532-536 define ``default`` as a
  client/config sentinel for standard routing, not a catalog tier id.
* ``protocol/src/openai_models.rs`` lines 902-913 resolve that sentinel to an
  omitted request field.
* ``core/tests/suite/model_switching.rs`` lines 829-863 assert omission on the
  final HTTP body even when model metadata advertises Fast as its default.
"""

import base64
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from litetui import model_transport as mt

FAST_MODEL_METADATA = {
    "supported_reasoning_levels": [
        {"effort": effort} for effort in ("low", "medium", "high")
    ],
    "service_tiers": [{"id": "priority", "name": "Fast"}],
    "default_service_tier": "priority",
}


def _auth_file(tmp_path):
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 3600}).encode()
    ).decode().rstrip("=")
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": f"header.{payload}.sig",
                    "account_id": "test-account",
                },
            }
        )
    )
    return path


def _completed_response() -> httpx.Response:
    event = {
        "type": "response.completed",
        "response": {"usage": {}, "output": []},
    }
    return httpx.Response(200, text=f"data: {json.dumps(event)}\n\n")


@pytest.mark.asyncio
async def test_main_codex_defaults_to_omitted_service_tier_despite_fast_metadata(
    tmp_path,
) -> None:
    captured = {}

    def handle(request):
        captured.update(json.loads(request.content))
        return _completed_response()

    transport = mt.OAuthTransport(
        "codex",
        credential_path=_auth_file(tmp_path),
        http_transport=httpx.MockTransport(handle),
        models={"gpt-test": FAST_MODEL_METADATA},
    )
    await transport.create(
        model="gpt-test",
        messages=[{"role": "user", "content": "hello"}],
        stream=False,
    )

    assert "service_tier" not in captured


def test_child_codex_defaults_to_omitted_service_tier_despite_fast_metadata(
    tmp_path, monkeypatch
) -> None:
    captured = {}
    auth_path = _auth_file(tmp_path)
    mock_transport = httpx.MockTransport(
        lambda request: (
            captured.update(json.loads(request.content)) or _completed_response()
        )
    )

    def init(self, provider, *, credential_path=None, http_transport=None, models=None):
        self.provider = provider
        self.credential_path = auth_path
        self.http_transport = mock_transport
        self.models = models
        self.prompt_cache_key = None

    monkeypatch.setattr(mt.OAuthTransport, "__init__", init)
    app = SimpleNamespace(
        backend=SimpleNamespace(
            remote=True,
            name="codex",
            models={"gpt-5.6-sol": FAST_MODEL_METADATA},
            reasoning_levels=lambda _model: ["low", "medium", "high"],
        )
    )
    mt.complete_sidecall(
        app,
        {
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
        },
    )

    assert "service_tier" not in captured


def test_unrelated_request_overrides_cannot_add_a_default_priority_tier() -> None:
    body = mt.codex_request(
        {
            "model": "gpt-test",
            "messages": [],
            "extra_body": {"reasoning_effort": "high"},
        }
    )

    assert body["reasoning"]["effort"] == "high"
    assert "service_tier" not in body
