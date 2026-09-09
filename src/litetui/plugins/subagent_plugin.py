"""Subagent tool: a one-shot chat completion in its own LM Studio slot.

No tools, no parent history — the child sees only its own prompt (and an
optional system message). The loaded model pool is SHARED across live slots,
so the settings cap (subagent_max_tokens, default 20k) is the ceiling.

Thinking is OFF by default (reasoning_effort "none" on the wire, same as the
app's own "off" level). Pass think=true when chain-of-thought is wanted.

Backgroundable (tasks.backgroundable reads the schema's `background` prop),
so it rides the same T499/T517 path as bash: explicit flag or auto-promotion.
"""
from __future__ import annotations

import json
import urllib.request

from litetui import tasks as tasks_mod
from litetui import tool_schemas
from litetui.plugins import PluginManifest
from litetui.tool_policy import NETWORK_READ_POLICY

SPEC = tool_schemas.load("subagent")


def _make_runner(app):
    def tool_subagent(args: dict) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "[error] prompt is required"
        system = (args.get("system") or "").strip() or None
        model = (args.get("model") or "").strip() or getattr(app, "model_id", None) or "local-model"
        think = bool(args.get("think", False))
        cap = getattr(getattr(app, "settings", None), "subagent_max_tokens", 20000) or 20000
        max_tokens = min(int(args.get("max_tokens") or cap), cap)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        host = getattr(getattr(app, "settings", None), "lm_host", "http://localhost:1234")
        url = f"{host.rstrip('/')}/v1/chat/completions"
        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if not think:
            payload["reasoning_effort"] = "none"
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            return f"[error] {type(e).__name__}: {e}"

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        usage = data.get("usage") or {}
        tokens = usage.get("completion_tokens")
        tok_display = tokens if tokens is not None else "?"

        task = tasks_mod.CURRENT.get(None)
        if task is not None and tokens is not None:
            task.tokens = tokens

        if not text and reasoning:
            tail = reasoning[-2000:] if len(reasoning) > 2000 else reasoning
            return (
                f"[subagent · {tok_display} tokens · model {model} · "
                f"WARNING: all tokens went to reasoning, content empty — "
                f"retry with think=false or a higher max_tokens]\n\n"
                f"Reasoning tail:\n{tail}"
            )
        if not text:
            return f"[subagent · {tok_display} tokens · model {model} · empty response]"
        return f"{text}\n\n[subagent · {tok_display} tokens · model {model}]"

    return tool_subagent


def _register(ctx) -> None:
    ctx.tool(SPEC, _make_runner(ctx.app), policy=NETWORK_READ_POLICY)


PLUGIN = PluginManifest(id="subagent", register=_register)
