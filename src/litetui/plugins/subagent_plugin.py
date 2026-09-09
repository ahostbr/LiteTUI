"""Subagent tool: a one-shot chat completion in its own LM Studio slot.

No tools, no parent history — the child sees only its own prompt (and an
optional system message). The loaded model pool is SHARED across live slots,
so the ceiling is the app's own thinking-safe budget, settings.compact_max_tokens
(Ryan 2026-09-08 21:0x: "litetui already has a think token budget set reuse
that for subagents its the same model running") — the knob the tool-result
summariser side call reuses too, never a third literal.

Thinking is OFF by default (reasoning_effort "none" on the wire, same as the
app's own "off" level). Pass think=true when chain-of-thought is wanted.

Files are read by the PLUGIN (not the model) and appended as fenced blocks —
the prompt stays short and the tool_call renders instantly.

Backgroundable (tasks.backgroundable reads the schema's `background` prop),
so it rides the same T499/T517 path as bash: explicit flag or auto-promotion.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from litetui import tasks as tasks_mod
from litetui import tool_schemas
from litetui.plugins import PluginManifest
from litetui.tool_policy import NETWORK_READ_POLICY

SPEC = tool_schemas.load("subagent")

FILE_CAP = 50_000


def _read_files(paths: list) -> str:
    blocks = []
    for raw in paths:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            if len(text) > FILE_CAP:
                text = text[:FILE_CAP] + f"\n[... truncated at {FILE_CAP} chars ...]"
            blocks.append(f"--- {p.name} ---\n{text}")
        except Exception as e:
            blocks.append(f"--- {raw} ---\n[error reading file: {type(e).__name__}: {e}]")
    return "\n\n".join(blocks)


def _make_runner(app):
    def tool_subagent(args: dict) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "[error] prompt is required"
        system = (args.get("system") or "").strip() or None
        model = (args.get("model") or "").strip() or getattr(app, "model_id", None) or "local-model"
        think = bool(args.get("think", False))
        file_paths = args.get("files") or []
        cap = getattr(getattr(app, "settings", None), "compact_max_tokens", 12288) or 12288
        max_tokens = min(int(args.get("max_tokens") or cap), cap)

        user_content = prompt
        if file_paths:
            user_content = f"{prompt}\n\n{_read_files(file_paths)}"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})

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
