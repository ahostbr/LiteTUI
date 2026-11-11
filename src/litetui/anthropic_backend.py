"""Paid Anthropic Messages API; independent of Claude Agent/OAuth.

LiteTUI owns tools, history and compaction. Model IDs and capabilities come from
GET /v1/models, never the Claude CLI catalogue. Credentials are resolved for each
request and are never copied into conversation settings or error messages.

Protocol: https://platform.claude.com/docs/en/api/overview
Version: https://platform.claude.com/docs/en/api/versioning
"""
from __future__ import annotations

import hashlib
import json
import os

import httpx

from litetui import anthropic_messages
from litetui.llm_backend import ModelRow
from litetui.model_transport import (
    ProviderError,
    _close_http,
    collect,
)

API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2026-10-21"


def api_key(settings) -> str:
    from litetui.secret_at_rest import is_sealed

    key = (getattr(settings, "anthropic_api_key", "") or "").strip()
    if is_sealed(key):
        # Still sealed after load: encrypted for another Windows account/machine.
        raise ProviderError(
            "The saved Anthropic API key cannot be decrypted by this Windows account. "
            "Re-enter it in /settings → Anthropic API, then /reconnect."
        )
    key = key or os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ProviderError(
            "Anthropic needs an API key. Set it in /settings → Anthropic API, "
            "or set ANTHROPIC_API_KEY, then /reconnect. Claude OAuth is separate."
        )
    return key


def _headers(settings):
    headers = {"Authorization": "Bearer " + api_key(settings),
               "anthropic-version": API_VERSION, "Content-Type": "application/json"}
    workspace = (getattr(settings, "anthropic_workspace_id", "") or "").strip()
    workspace = workspace or os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    return headers


def _auth_identity(headers):
    # Compare resolved credentials without retaining/logging plaintext in the catalog.
    return hashlib.sha256(json.dumps(headers, sort_keys=True).encode()).digest()


def _http_error(status):
    # Do not render an untrusted body: upstream diagnostics can echo credentials.
    if status == 401:
        detail = "API key was rejected. Check the key in /settings or ANTHROPIC_API_KEY."
    elif status == 403:
        detail = "access denied. Check API key permissions and the workspace ID."
    elif status == 429:
        detail = "rate or spend limit reached. Check Console limits, then retry later."
    elif status >= 500:
        detail = "service is unavailable. Try again later."
    else:
        detail = "request refused. Check the selected model, output budget and API account."
    return ProviderError(f"Anthropic {detail} (HTTP {status}). No fallback was used.")


def _supported(capabilities, name):
    value = capabilities.get(name) if isinstance(capabilities, dict) else None
    return isinstance(value, dict) and value.get("supported") is True


def _capabilities(model):
    caps = model.get("capabilities")
    return caps if isinstance(caps, dict) else {}


def _thinking_types(model):
    thinking = _capabilities(model).get("thinking")
    return thinking.get("types", {}) if isinstance(thinking, dict) else {}


def _reasoning_levels(model):
    effort = _capabilities(model).get("effort") or {}
    return (["none"] if _supported(_thinking_types(model), "disabled") else []) + [
        level for level in ("low", "medium", "high", "xhigh", "max") if _supported(effort, level)]


class AnthropicBackend:
    name = "anthropic"
    label = "Anthropic API"
    remote = True
    attached = True

    def __init__(self, settings, *, http_transport=None):
        self.settings = settings
        self.http_transport = http_transport
        # Publish identity and contents together. Sidecalls can use another thread.
        self._catalog = (None, {})

    @property
    def models(self):
        return self._catalog[1]

    def set_settings(self, settings):
        if any(getattr(self.settings, name, "") != getattr(settings, name, "")
               for name in ("anthropic_api_key", "anthropic_workspace_id")):
            self._catalog = (None, {})
        self.settings = settings

    def base_url(self):
        # Identity only. for_app and child/sidecall dispatch use Messages, not OpenAI.
        return API_BASE

    def host(self):
        return API_BASE

    async def ensure_running(self):
        api_key(self.settings)
        return "ok"

    async def _fetch_models(self, headers):
        models, cursors = {}, set()
        params = {"limit": "1000"}
        try:
            async with httpx.AsyncClient(transport=self.http_transport, timeout=20) as client:
                while True:
                    response = await client.get(API_BASE + "/models", headers=headers, params=params)
                    if not response.is_success:
                        raise _http_error(response.status_code)
                    page = response.json()
                    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                        raise TypeError("invalid model catalogue")
                    for model in page["data"]:
                        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"]:
                            raise ValueError("invalid model entry")
                        if model.get("lifecycle") != "retired":
                            models[model["id"]] = model
                    if not page.get("has_more"):
                        break
                    cursor = page.get("last_id")
                    if not isinstance(cursor, str) or not cursor or cursor in cursors or len(cursors) >= 100:
                        raise ValueError("invalid model cursor")
                    cursors.add(cursor)
                    params["after_id"] = cursor
        except httpx.HTTPError:
            raise ProviderError("Could not reach the Anthropic model catalog. Check the connection and /reconnect.") from None
        except (ValueError, TypeError):
            raise ProviderError("Anthropic returned an unreadable model catalog. Try /reconnect.") from None
        if not models:
            raise ProviderError("Anthropic returned no available models. Check API account access and /reconnect.")
        return models

    async def _refresh_catalog(self, headers):
        try:
            models = await self._fetch_models(headers)
        except BaseException:
            self._catalog = (None, {})
            raise
        self._catalog = (_auth_identity(headers), models)
        return models

    async def list_models(self):
        models = await self._refresh_catalog(_headers(self.settings))
        return [ModelRow(key=key, path=None, source=self.label, loaded=True,
                         modalities=("text", "image") if _supported(_capabilities(model), "image_input") else ("text",))
                for key, model in models.items()]

    async def model_info(self, key):
        model = self.models.get(key)
        if model is None:
            return None
        context = model.get("max_input_tokens")
        context = context if type(context) is int and context > 0 else None
        return context, "vlm" if _supported(_capabilities(model), "image_input") else "llm", True

    async def _request_model(self, key):
        headers = _headers(self.settings)
        identity, models = self._catalog
        if not models or identity != _auth_identity(headers):
            models = await self._refresh_catalog(headers)
        if key not in models:
            raise ProviderError("Choose an available Anthropic API model with /model. No fallback was used.")
        # Request-local capability/auth snapshot, unaffected by concurrent refreshes.
        return models[key], headers

    async def ensure_chat_ready(self, key):
        await self._request_model(key)

    def reasoning_levels(self, key):
        return _reasoning_levels(self.models.get(key, {}))

    def request_overrides(self, key):
        value = (self.settings.model_infer_overrides or {}).get(key, {}).get("reasoning_effort")
        return {"reasoning_effort": value} if value else {}

    def transport(self):
        return AnthropicTransport(self)

    async def complete(self, payload):
        """One-shot child/summary boundary, with no parent session or tools."""
        request = dict(payload)
        request.pop("chat_template_kwargs", None)
        effort = request.pop("reasoning_effort", None)
        await self.ensure_chat_ready(request["model"])
        levels = self.reasoning_levels(request["model"])
        # The host's child 'none' means minimum supported effort, not a promise
        # to disable thinking on models that require it.
        if effort == "none" and "none" not in levels:
            effort = levels[0] if levels else None
        if effort is not None:
            request["extra_body"] = {**request.get("extra_body", {}), "reasoning_effort": effort}
        request["stream"] = False
        result = await self.transport().create(purpose="sidecall", **request)
        msg = result.choices[0].message
        return {"choices": [{"message": {"content": msg.content, "reasoning_content": msg.reasoning_content}}],
                "usage": vars(result.usage) if result.usage else {}}

    async def load(self, key, **kwargs):
        raise ProviderError("Anthropic runs remotely. Select a model with /model; loading is not needed.")

    async def unload(self, key):
        raise ProviderError("Anthropic models do not occupy local model memory.")

    def shutdown(self):
        pass  # Requests own their clients; no persistent process or connection.

    def seat_snapshot(self, key):
        return None

    def empty_state_hint(self):
        return "set an Anthropic API key in /settings or ANTHROPIC_API_KEY, then /reconnect"


class AnthropicTransport:
    def __init__(self, backend):
        self.backend = backend

    async def create(self, *, purpose="turn", retry_notice=None, retry_budget=None, **kwargs):
        backend = self.backend
        model, headers = await backend._request_model(kwargs["model"])
        body = anthropic_messages.request(kwargs)
        limit = model.get("max_tokens")
        if type(body["max_tokens"]) is not int or body["max_tokens"] <= 0:
            raise ProviderError("Anthropic requires a positive output token budget in /settings.")
        if type(limit) is int and limit > 0 and body["max_tokens"] > limit:
            raise ProviderError(f"This Anthropic model supports at most {limit} output tokens. Lower the reply budget in /settings.")
        effort = (kwargs.get("extra_body") or {}).get("reasoning_effort")
        if effort and effort != "default":
            if effort not in _reasoning_levels(model):
                raise ProviderError("This Anthropic model does not support that reasoning effort. Choose one with /think default or /modelcfg.")
            if effort == "none":
                body["thinking"] = {"type": "disabled"}
            else:
                body["output_config"] = {"effort": effort}
                if _supported(_thinking_types(model), "adaptive"):
                    body["thinking"] = {"type": "adaptive"}
        if (body.get("thinking", {}).get("type") == "adaptive"
                and body.get("tool_choice", {}).get("type") in ("any", "tool")):
            raise ProviderError("Anthropic forced tool choice is incompatible with adaptive thinking. Use automatic tool choice or disable thinking on a model that supports it.")
        client = httpx.AsyncClient(transport=backend.http_transport, timeout=httpx.Timeout(120, connect=20))
        response = None
        stream_owns_client = False
        try:
            request = client.build_request("POST", API_BASE + "/messages", headers=headers, json=body)
            response = await client.send(request, stream=True)
            if not response.is_success:
                raise _http_error(response.status_code)
            result = anthropic_messages.MessageStream(response, client, kwargs["model"], kwargs.get("tools") or [],
                                                      secrets=(headers["Authorization"].removeprefix("Bearer "),))
            stream_owns_client = True
            return result if kwargs.get("stream", False) else await collect(result)
        except BaseException as error:
            if not stream_owns_client:
                try:
                    if response is not None:
                        await _close_http(response, "anthropic", "response")
                finally:
                    await _close_http(client, "anthropic", "client")
            if isinstance(error, httpx.HTTPError):
                raise ProviderError("Could not connect to Anthropic. Check the connection and retry; no fallback was used.") from None
            raise
