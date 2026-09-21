"""One inference request, never an agent loop. OAuth stores are read-only.

Wire-format reference: pi-ai at 421c03efb (MIT). No Pi runtime dependency.
The public chunk shape matches the existing LiteTUI stream consumer. Opaque
reasoning blocks travel beside messages, scoped to provider AND model.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Protocol

import httpx

from litetui.llm_backend import BackendError

OAUTH_PROVIDERS = ("codex",)  # Claude is enabled only after live acceptance.
URLS = {
    "codex": "https://chatgpt.com/backend-api/codex/responses",
    "claude": "https://api.anthropic.com/v1/messages",
}


class ProviderError(BackendError):
    """Safe to render and log: no upstream body, headers, or credentials."""


class ModelTransport(Protocol):
    async def create(self, *, purpose: str = "turn", **kwargs): ...


@dataclass(frozen=True)
class Credentials:
    access: str = field(repr=False)
    account_id: str = field(default="", repr=False)


def credential_path(provider: str) -> Path:
    if provider == "codex":
        return (
            Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
        )
    return (
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        / ".credentials.json"
    )


def login_error(provider: str) -> ProviderError:
    command = "codex login" if provider == "codex" else "claude auth login"
    return ProviderError(
        f"{provider.title()} needs a current subscription login. Run `{command}`, then /reconnect. API keys are not supported."
    )


def read_credentials(provider: str, path: Path | None = None) -> Credentials:
    try:
        obj = json.loads(
            (path or credential_path(provider)).read_text(encoding="utf-8")
        )
        if not isinstance(obj, dict):
            raise TypeError("invalid store")
        if provider == "codex":
            if obj.get("auth_mode") != "chatgpt" or obj.get("OPENAI_API_KEY"):
                raise ValueError("subscription required")
            tokens = obj["tokens"]
            access, account = tokens["access_token"], tokens["account_id"]
            if (
                not isinstance(access, str)
                or not isinstance(account, str)
                or not account
            ):
                raise ValueError("missing credentials")
            payload = access.split(".")[1]
            exp = json.loads(
                base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            )["exp"]
        elif provider == "claude":
            tokens = obj["claudeAiOauth"]
            if not isinstance(tokens, dict):
                raise TypeError("invalid store")
            access, account = tokens["accessToken"], ""
            if not tokens.get("subscriptionType") or "user:inference" not in tokens.get(
                "scopes", []
            ):
                raise ValueError("subscription required")
            if not isinstance(access, str) or not access.startswith("sk-ant-oat"):
                raise ValueError("OAuth token required")
            exp = float(tokens["expiresAt"]) / 1000
        else:
            raise ValueError("unknown provider")
        if float(exp) <= time.time() + 30:
            raise ValueError("expired")
        return Credentials(access, account)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise login_error(provider) from None


def auth_status(provider: str) -> str:
    try:
        read_credentials(provider)
        return "OAuth signed in"
    except ProviderError:
        return "subscription login required"


def _metadata(message: dict, provider: str, model: str) -> list:
    meta = message.get("provider_metadata") or {}
    if meta.get("provider") == provider and meta.get("model") == model:
        return copy.deepcopy(meta.get("items") or [])
    return []


def codex_request(kwargs: dict) -> dict:
    model = kwargs["model"]
    items, instructions = [], []
    for m in kwargs["messages"]:
        role, content = m["role"], m.get("content")
        if role == "system":
            instructions.append(str(content or ""))
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m["tool_call_id"],
                    "output": str(content or ""),
                }
            )
            continue
        items.extend(_metadata(m, "codex", model))
        blocks = []
        for b in (
            content
            if isinstance(content, list)
            else ([{"type": "text", "text": content}] if content else [])
        ):
            if b.get("type") == "image_url" and role == "user":
                blocks.append(
                    {"type": "input_image", "image_url": b["image_url"]["url"]}
                )
            elif b.get("type") == "text":
                blocks.append(
                    {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": b["text"],
                    }
                )
        if blocks:
            items.append({"role": role, "content": blocks})
        for call in m.get("tool_calls") or []:
            items.append(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            )
    body = {
        "model": model,
        "instructions": "\n\n".join(instructions),
        "input": items,
        "store": False,
        "stream": True,
        "include": ["reasoning.encrypted_content"],
        "tool_choice": kwargs.get("tool_choice", "auto"),
        "parallel_tool_calls": True,
    }
    if kwargs.get("tools"):
        body["tools"] = [
            {"type": "function", **t["function"], "strict": False}
            for t in kwargs["tools"]
        ]
    effort = (kwargs.get("extra_body") or {}).get("reasoning_effort")
    if effort:
        body["reasoning"] = {"effort": effort, "summary": "auto"}
    if kwargs.get("prompt_cache_key"):
        body["prompt_cache_key"] = kwargs["prompt_cache_key"]
    return body


_CLAUDE_NAMES = {
    n.lower(): n
    for n in (
        "Read",
        "Write",
        "Edit",
        "Bash",
        "Grep",
        "Glob",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
    )
}


def claude_request(kwargs: dict) -> dict:
    messages: list[dict] = []
    system: list[dict] = []
    for m in kwargs["messages"]:
        role, content = m["role"], m.get("content")
        if role == "system":
            system.append({"type": "text", "text": str(content or "")})
            continue
        blocks = _metadata(m, "claude", kwargs["model"])
        if role == "tool":
            role = "user"
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": str(content or ""),
                }
            )
        else:
            for b in (
                content
                if isinstance(content, list)
                else ([{"type": "text", "text": content}] if content else [])
            ):
                if b.get("type") == "text":
                    blocks.append({"type": "text", "text": b["text"]})
                elif b.get("type") == "image_url":
                    url = b["image_url"]["url"]
                    if url.startswith("data:"):
                        header, data = url.split(",", 1)
                        blocks.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": header[5:].split(";")[0],
                                    "data": data,
                                },
                            }
                        )
                    else:
                        blocks.append(
                            {"type": "image", "source": {"type": "url", "url": url}}
                        )
            for c in m.get("tool_calls") or []:
                name = c["function"]["name"]
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": _CLAUDE_NAMES.get(name.lower(), name),
                        "input": json.loads(c["function"]["arguments"]),
                    }
                )
        if blocks:
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"].extend(blocks)
            else:
                messages.append({"role": role, "content": blocks})
    body = {
        "model": kwargs["model"],
        "messages": messages,
        "system": system,
        "max_tokens": kwargs.get("max_tokens", 4096),
        "stream": True,
    }
    if kwargs.get("tools"):
        body["tools"] = [
            {
                "name": _CLAUDE_NAMES.get(
                    t["function"]["name"].lower(), t["function"]["name"]
                ),
                "description": t["function"].get("description", ""),
                "input_schema": t["function"]["parameters"],
            }
            for t in kwargs["tools"]
        ]
    return body


def _chunk(text=None, reasoning=None, call=None, usage=None, metadata=None):
    return NS(
        choices=[
            NS(
                delta=NS(
                    content=text,
                    reasoning_content=reasoning,
                    tool_calls=[call] if call else [],
                )
            )
        ],
        usage=usage,
        provider_metadata=metadata,
    )


def _call(index, id=None, name=None, arguments=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=arguments))


def numeric_usage(value):
    """Retain numeric usage metadata only; unknown values remain unknown."""
    import math

    if isinstance(value, dict):
        return {k: numeric_usage(v) for k, v in value.items()
                if isinstance(v, dict) or (type(v) in (int, float) and math.isfinite(v))}
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _usage(raw, provider):
    inp = raw.get("input_tokens", 0)
    if provider == "claude":
        inp += raw.get("cache_read_input_tokens", 0) + raw.get(
            "cache_creation_input_tokens", 0
        )
    out = raw.get("output_tokens", 0)
    details = numeric_usage(raw.get("input_tokens_details"))
    numeric = numeric_usage(raw)
    return NS(prompt_tokens=inp, completion_tokens=out, total_tokens=inp + out,
              cached_tokens=(details or {}).get("cached_tokens"),
              cache_write_tokens=(details or {}).get("cache_write_tokens"),
              input_tokens_details=details, usage_details=numeric)


class ResponseStream:
    def __init__(self, response, client, provider, model, tools):
        self.response, self.client = response, client
        self.provider, self.model = provider, model
        self.names = {
            t["function"]["name"].lower(): t["function"]["name"] for t in tools
        }

    async def close(self):
        await self.response.aclose()
        await self.client.aclose()

    async def __aiter__(self):
        completed, usage, blocks = False, {}, {}
        # Codex sends reasoning as SEVERAL summary parts per turn; see the
        # comment at response.reasoning_summary_part.added below.
        summary_part_seen = False
        try:
            async for line in self.response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    e = json.loads(data)
                    kind = e.get("type", "")
                    if kind in ("error", "response.failed", "response.incomplete"):
                        raise ProviderError(
                            f"{self.provider.title()} did not complete the response. Retry or reduce context."
                        )
                    if self.provider == "codex":
                        idx = e.get("output_index", 0)
                        if kind == "response.output_text.delta":
                            yield _chunk(text=e["delta"])
                        elif kind == "response.reasoning_summary_text.delta":
                            yield _chunk(reasoning=e["delta"])
                        elif kind == "response.reasoning_summary_part.added":
                            """A part boundary is a LINE BREAK, and it only exists here.

                            Measured against one real stream from gpt-5.6-sol
                            (probe, 2026-09-11): each summary part arrives as
                            part.added -> summary_text.delta* ->
                            summary_text.done -> part.done, carrying
                            summary_index, and each part's text is a bold
                            heading such as "**Calculating smallest n for 100
                            trailing zeros**" with no trailing newline.
                            Forwarding only the deltas concatenates the parts
                            with NOTHING between them, so heading N's closing
                            ** abuts heading N+1's opening ** and the block
                            renders "**A****B****C**" on one line -- which is
                            exactly what Ryan photographed.

                            The flag, rather than `summary_index > 0`: a turn
                            can contain several reasoning ITEMS (one per tool
                            round), and summary_index restarts at 0 in each, so
                            an index test would re-fuse the first heading of
                            every item after the first.
                            """
                            if summary_part_seen:
                                yield _chunk(reasoning="\n\n")
                            summary_part_seen = True
                        elif (
                            kind == "response.output_item.added"
                            and e["item"]["type"] == "function_call"
                        ):
                            item = e["item"]
                            yield _chunk(
                                call=_call(
                                    idx,
                                    item["call_id"],
                                    item["name"],
                                    item.get("arguments", ""),
                                )
                            )
                        elif kind == "response.function_call_arguments.delta":
                            yield _chunk(call=_call(idx, arguments=e["delta"]))
                        elif kind == "response.completed":
                            completed = True
                            response = e["response"]
                            opaque = [
                                i
                                for i in response.get("output", [])
                                if i["type"] == "reasoning"
                            ]
                            yield _chunk(
                                usage=_usage(response.get("usage") or {}, "codex"),
                                metadata={
                                    "provider": "codex",
                                    "model": self.model,
                                    "items": opaque,
                                },
                            )
                    else:
                        idx = e.get("index", 0)
                        if kind == "message_start":
                            usage.update(e["message"].get("usage") or {})
                        elif kind == "content_block_start":
                            b = e["content_block"]
                            blocks[idx] = dict(b)
                            if b["type"] == "tool_use":
                                name = self.names.get(b["name"].lower(), b["name"])
                                yield _chunk(call=_call(idx, b["id"], name, ""))
                        elif kind == "content_block_delta":
                            d = e["delta"]
                            if d["type"] == "text_delta":
                                yield _chunk(text=d["text"])
                            elif d["type"] == "input_json_delta":
                                yield _chunk(
                                    call=_call(idx, arguments=d["partial_json"])
                                )
                            elif d["type"] == "thinking_delta":
                                blocks[idx]["thinking"] = (
                                    blocks[idx].get("thinking", "") + d["thinking"]
                                )
                                yield _chunk(reasoning=d["thinking"])
                            elif d["type"] == "signature_delta":
                                blocks[idx]["signature"] = (
                                    blocks[idx].get("signature", "") + d["signature"]
                                )
                        elif kind == "message_delta":
                            usage.update(e.get("usage") or {})
                            if e.get("delta", {}).get("stop_reason") == "max_tokens":
                                raise ProviderError(
                                    "Claude reached its response limit. Increase the output budget."
                                )
                        elif kind == "message_stop":
                            completed = True
                            opaque = [
                                b
                                for b in blocks.values()
                                if b["type"] in ("thinking", "redacted_thinking")
                            ]
                            yield _chunk(
                                usage=_usage(usage, "claude"),
                                metadata={
                                    "provider": "claude",
                                    "model": self.model,
                                    "items": opaque,
                                },
                            )
                except (ValueError, KeyError, TypeError):
                    raise ProviderError(
                        f"{self.provider.title()} returned an unreadable stream."
                    ) from None
            if not completed:
                raise ProviderError(
                    f"{self.provider.title()} stream ended before completion. Retry the turn."
                )
        except httpx.HTTPError:
            raise ProviderError(
                f"{self.provider.title()} connection was interrupted. Retry the turn."
            ) from None
        finally:
            await self.close()


async def collect(stream):
    text, reasoning, calls, metadata, usage = "", "", {}, None, None
    async for chunk in stream:
        d = chunk.choices[0].delta
        text += d.content or ""
        reasoning += d.reasoning_content or ""
        usage = chunk.usage or usage
        metadata = chunk.provider_metadata or metadata
        for c in d.tool_calls:
            acc = calls.setdefault(
                c.index, NS(id="", type="function", function=NS(name="", arguments=""))
            )
            acc.id = c.id or acc.id
            acc.function.name += c.function.name or ""
            acc.function.arguments += c.function.arguments or ""
    return NS(
        choices=[
            NS(
                message=NS(
                    content=text,
                    reasoning_content=reasoning,
                    tool_calls=list(calls.values()),
                    provider_metadata=metadata,
                )
            )
        ],
        usage=usage,
    )


class OAuthTransport:
    def __init__(
        self, provider, *, credential_path=None, http_transport=None, models=None,
        prompt_cache_key=None
    ):
        self.provider = provider
        self.credential_path = credential_path
        self.http_transport = http_transport
        self.models = models
        self.prompt_cache_key = prompt_cache_key

    def headers(self, credentials):
        headers = {
            "Authorization": "Bearer " + credentials.access,
            "Content-Type": "application/json",
        }
        if self.provider == "codex":
            headers.update(
                {"chatgpt-account-id": credentials.account_id, "originator": "litetui"}
            )
        else:
            headers.update(
                {
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                }
            )
        return headers

    async def create(self, *, purpose: str = "turn", **kwargs):
        """`purpose` NAMES WHAT THIS CALL IS FOR, and it never reaches the wire.

        🔴 T821. A test counted `create` calls to assert how many COMPLETIONS
        one turn costs. It was right on 2026-09-12 (4e873f7) and red by
        2026-09-16, when `_kick_card_summary` (f2571bf, Ryan's collapsed-card
        title) started borrowing this same transport. Nothing on either side
        was edited.

            A COUNTER OVER A SHARED TRANSPORT COUNTS EVERY FEATURE THAT USES
            IT. The arm named 'completion requests' and measured 'calls
            through the client' — the same number on the day it was written,
            which is exactly why nobody noticed.

        Captured as a NAMED parameter so it cannot reach the HTTP client, and
        defaulted to the turn, so an untagged caller keeps today's meaning.
        """
        if self.provider == "codex" and self.prompt_cache_key:
            kwargs["prompt_cache_key"] = self.prompt_cache_key
        credentials = read_credentials(self.provider, self.credential_path)
        if self.models is not None:
            model = self.models.get(kwargs["model"])
            if not model:
                raise ProviderError("Choose an available Codex model with /model.")
            effort = (kwargs.get("extra_body") or {}).get("reasoning_effort")
            levels = [r["effort"] for r in model.get("supported_reasoning_levels", [])]
            if effort and effort not in levels:
                raise ProviderError(
                    "This Codex model does not support that reasoning effort. Choose one with /modelcfg."
                )
        body = (
            codex_request(kwargs)
            if self.provider == "codex"
            else claude_request(kwargs)
        )
        client = httpx.AsyncClient(
            transport=self.http_transport, timeout=httpx.Timeout(120, connect=20)
        )
        try:
            for attempt in range(2):
                req = client.build_request(
                    "POST",
                    URLS[self.provider],
                    headers=self.headers(credentials),
                    json=body,
                )
                response = await client.send(req, stream=True)
                if response.status_code == 401:
                    await response.aclose()
                    fresh = read_credentials(self.provider, self.credential_path)
                    if attempt or fresh == credentials:
                        raise login_error(self.provider)
                    credentials = fresh
                    continue
                if not response.is_success:
                    status = response.status_code
                    await response.aclose()
                    if status == 429:
                        raise ProviderError(
                            f"{self.provider.title()} usage limit reached. Wait and retry; no fallback was used."
                        )
                    raise ProviderError(
                        f"{self.provider.title()} refused the request (HTTP {status}). Check account access and the selected model."
                    )
                result = ResponseStream(
                    response,
                    client,
                    self.provider,
                    kwargs["model"],
                    kwargs.get("tools") or [],
                )
                return result if kwargs.get("stream", False) else await collect(result)
            raise login_error(self.provider)
        except BaseException as error:
            await client.aclose()
            if isinstance(error, httpx.HTTPError):
                raise ProviderError(
                    f"Could not connect to {self.provider.title()}. Check the connection and retry."
                ) from None
            raise


def _refuse_unsupported_local_lm(backend, *, remote_marker=None):
    """Fail-closed request-level guard for LM Studio's JIT-on-inference (WS3).

    LM Studio JIT-loads a cold model into local VRAM on the FIRST inference
    request itself — not through our load path — so the request IS a load. A
    resident snapshot is not a safe bypass (the model can evict between the
    check and the request), and a safe per-request usage admission does not exist
    yet. So a LOCAL LM Studio inference is refused BEFORE any HTTP, unconditionally
    (cold, warm, or even once calibration exists), with an actionable reason and
    NO stream/lifecycle wrapper. A trusted REMOTE endpoint (its VRAM is not ours)
    passes; an UNKNOWN locality blocks. Other backends (llama, ninfer, codex) are
    not request-JIT — their loads are gated at the load/spawn path — so they pass.
    """
    from litetui.llm_backend import LMStudioBackend
    if not isinstance(backend, LMStudioBackend):
        return
    from litetui.resource_admission_install import classify_locality
    from litetui.model_resource_session import AdmissionBlocked
    scope = classify_locality(backend, remote_marker=remote_marker)
    if scope == "remote":
        return
    if scope == "local":
        raise AdmissionBlocked(
            "BLOCKED: local LM Studio inference is refused — it JIT-loads a model "
            "into VRAM on the request, and JIT-safe per-request usage admission is "
            "not implemented yet. Deliberate temporary block until the usage/lease "
            "protocol lands."
        )
    raise AdmissionBlocked(
        "BLOCKED: cannot confirm this LM Studio endpoint is local; the request is "
        "refused. Remote-endpoint admission is not supported yet."
    )


class OpenAITransport:
    def __init__(self, client, *, backend=None, remote_marker=None):
        # `backend` is optional so a standalone OpenAITransport(client) keeps
        # working; production for_app always binds the current backend so the
        # request-level LM Studio JIT guard can fire.
        self.client = client
        self.backend = backend
        self.remote_marker = remote_marker

    async def create(self, *, purpose: str = "turn", **kwargs):
        _refuse_unsupported_local_lm(self.backend, remote_marker=self.remote_marker)
        kwargs = dict(kwargs)
        kwargs["messages"] = [
            {k: v for k, v in m.items() if k not in ("provider_metadata", "codex_delivery")}
            for m in kwargs["messages"]
        ]
        return await self.client.chat.completions.create(**kwargs)


@dataclass(frozen=True)
class ClientBinding:
    """Immutable authorization created at client creation: THIS client object, for
    THIS backend TYPE (its admission scope / JIT classification), at THIS endpoint.

    for_app refuses unless the CURRENT (app.client, app.backend) still matches the
    record. This catches (a) a client replaced by hand, (b) a same-URL backend-TYPE
    swap (LM<->llama at one address changes JIT classification), and (c) an endpoint
    mutation. It is NOT a caller-editable string: it binds the client OBJECT and the
    backend CLASS, and a same-endpoint reconnect must re-bind (bind_client) to
    re-authorize the kept client for the rebuilt backend.
    """
    client: object
    backend_type: type
    endpoint: str


def bind_client(client, backend) -> ClientBinding:
    """Record the client<->backend binding. Call at every client (re)build and
    store the result on app._client_binding (see app.py factory sites)."""
    return ClientBinding(client, type(backend), backend.base_url().rstrip("/"))


def for_app(app) -> ModelTransport:
    if getattr(getattr(app, "backend", None), "name", None) in OAUTH_PROVIDERS:
        if hasattr(app.backend, "app_server"):
            from litetui.codex_app_server import AppServerTransport
            transport = getattr(app.backend, "_transport", None)
            if transport is None:
                transport = AppServerTransport(app.backend.app_server, app)
                app.backend._transport = transport
            return transport
        return OAuthTransport(
            app.backend.name, models=app.backend.models,
            prompt_cache_key=getattr(app, "convo_id", None),
        )
    # Refuse a stale client<->backend pair rather than misroute a request. The
    # binding is recorded at client creation (app.py factory sites). Production
    # for_app REQUIRES it: a missing binding fails closed. A mismatch — client
    # replaced by hand, a same-URL backend-TYPE swap, or an endpoint mutation —
    # refuses. (A standalone OpenAITransport(client) built directly, without
    # for_app, is the optional test path and carries no binding.)
    backend = app.backend
    binding = getattr(app, "_client_binding", None)
    from litetui.model_resource_session import AdmissionBlocked
    if binding is None:
        raise AdmissionBlocked(
            "BLOCKED: no client<->backend binding is recorded; refusing to send "
            "rather than route an unverified client."
        )
    if (binding.client is not app.client
            or binding.backend_type is not type(backend)
            or binding.endpoint != backend.base_url().rstrip("/")):
        raise AdmissionBlocked(
            "BLOCKED: the OpenAI client is a stale pair for the current backend "
            f"(bound {binding.backend_type.__name__}@{binding.endpoint}, now "
            f"{type(backend).__name__}@{backend.base_url().rstrip('/')}). Rebuild "
            "the client through the factory before sending."
        )
    return OpenAITransport(app.client, backend=backend)


def complete_sidecall(app, payload, *, opener=urllib.request.urlopen):
    """Synchronous tool-worker boundary; never shares an async HTTP connection.

    Local wire behavior stays compatible. Remote side calls use the same transport
    as chat, without tools or parent history. Called only inside the tool worker.
    """
    backend = getattr(app, "backend", None)
    if getattr(backend, "remote", False):

        async def run():
            request = dict(payload)
            request.pop("chat_template_kwargs", None)
            effort = request.pop("reasoning_effort", None)
            levels = backend.reasoning_levels(request["model"])
            if not levels:
                raise ProviderError(
                    "Select a Codex model for the subagent with /model or its model argument."
                )
            # Local think=false means minimum thinking; Codex models may have
            # no no-thinking mode. The result explicitly labels this below.
            wire_effort = levels[0] if effort == "none" else (effort or "medium")
            if wire_effort not in levels:
                raise ProviderError(
                    f"This Codex model does not support reasoning effort "
                    f"{wire_effort!r}; no fallback was used."
                )
            request["extra_body"] = {"reasoning_effort": wire_effort}
            if hasattr(backend, "app_server"):
                from litetui.codex_app_server import AppServer, AppServerTransport
                server = AppServer()
                try:
                    result = await AppServerTransport(server).create(**request)
                finally:
                    await server.close()
            else:
                result = await OAuthTransport(backend.name).create(**request)
            msg = result.choices[0].message
            return {
                "choices": [
                    {
                        "message": {
                            "content": msg.content,
                            "reasoning_content": msg.reasoning_content,
                        }
                    }
                ],
                "usage": vars(result.usage) if result.usage else {},
            }

        return asyncio.run(run())
    # Local sync sidecall: refuse LM Studio JIT-on-inference BEFORE the urllib
    # request (synchronous raise, no event loop needed).
    _refuse_unsupported_local_lm(backend)
    url = backend.base_url() if backend else app.settings.lm_host.rstrip("/") + "/v1"
    req = urllib.request.Request(
        url + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(req, timeout=600) as response:
        return json.loads(response.read())
