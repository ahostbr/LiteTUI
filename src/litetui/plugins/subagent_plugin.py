"""Subagent tool: an independent one-shot completion on the active backend.

No tools, no parent history — the child sees only its own prompt (and an
optional system message). The token ceiling is the app's own thinking-safe budget, settings.compact_max_tokens
(Ryan 2026-09-08 21:0x: "litetui already has a think token budget set reuse
that for subagents its the same model running") — the knob the tool-result
summariser side call reuses too, never a third literal.

Thinking defaults to off locally and the minimum supported effort remotely.
`think=true` preserves the legacy medium remote request. Callers that need a
specific Codex level pass `reasoning_effort` explicitly; it is never inherited
from or applied to the parent turn.

Files are read by the PLUGIN (not the model) and appended as fenced blocks —
the prompt stays short and the tool_call renders instantly.

Backgroundable (tasks.backgroundable reads the schema's `background` prop),
so it rides the same T499/T517 path as bash: explicit flag or auto-promotion.
"""
from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path

from litetui import model_transport, tool_schemas
from litetui import tasks as tasks_mod
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
        except Exception as e:  # noqa: BLE001 - tool boundary returns failures as visible results
            blocks.append(f"--- {raw} ---\n[error reading file: {type(e).__name__}: {e}]")
    return "\n\n".join(blocks)


def _resolve_model(app, explicit):
    """Resolve against the active backend; refresh local residency without loading.

    🔴 T611 - THE EXPLICIT ARGUMENT IS CHECKED TOO, AND THAT IS THE POINT.
    T609 made the DEFAULTS (persisted slot, current model) resident-only on
    local so the fallback could never name a model LM Studio would JIT-load.
    `model` is not a user's choice though: the PARENT MODEL writes it mid-turn,
    so "explicit" here means "a token the LLM emitted", and letting it through
    left the whole T594 rule reachable by one tool call. It still wins over
    every default - it is just held to the same residency law.
    """
    backend = getattr(app, "backend", None)
    remote = getattr(backend, "remote", False)
    rows = getattr(app, "model_rows", {})
    if backend is not None and not remote:
        # Query residency for each call, not a UI snapshot from before a switch.
        resident = getattr(backend, "loaded_models", None)
        listing = getattr(backend, "list_models", None)
        if resident is not None:
            loaded = set(resident())
        elif listing is not None:
            loaded = {r.key for r in asyncio.run(listing()) if r.loaded}
        else:
            loaded = {key for key, row in rows.items() if row.loaded}
    else:
        loaded = {key for key, row in rows.items() if row.loaded}

    def valid(model):
        if not model:
            return False
        if remote:
            return model in getattr(backend, "models", {})
        return model in loaded

    if explicit:
        # LOCAL ONLY, deliberately. A remote backend loads nothing, so an
        # explicit remote id costs at most one 404 and refusing it here would
        # be a different card's change (test_explicit_model_wins pins that).
        if remote or valid(explicit):
            return explicit
        raise model_transport.ProviderError(
            f"The subagent asked for {explicit!r}, which is not loaded. "
            f"Available: {', '.join(sorted(loaded)) or '(none)'}."
        )

    persisted = getattr(getattr(app, "settings", None), "subagent_model", None)
    current = getattr(app, "model_id", None)
    for model in (persisted, current):
        if valid(model):
            return model
    if remote:
        raise model_transport.ProviderError(
            "Select a Codex model for the subagent with /model or its model argument."
        )
    raise model_transport.ProviderError("Select an available loaded model for the subagent with /model or its model argument.")


def _make_runner(app):
    def tool_subagent(args: dict) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "[error] prompt is required"
        system = (args.get("system") or "").strip() or None
        try:
            model = _resolve_model(app, (args.get("model") or "").strip())
        except Exception as e:  # noqa: BLE001 - tool boundary returns failures as visible results
            return f"[error] {type(e).__name__}: {e}"
        think = bool(args.get("think", False))
        effort = (args.get("reasoning_effort") or "").strip().lower() or None
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

        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if effort is not None:
            # Explicit child-only effort. The transport validates it against
            # this model's provider catalog; unsupported values refuse rather
            # than silently falling back to medium.
            payload["reasoning_effort"] = effort
        elif not think:
            payload["reasoning_effort"] = "none"
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            data = model_transport.complete_sidecall(app, payload, opener=urllib.request.urlopen)
        except Exception as e:  # noqa: BLE001 - tool boundary returns failures as visible results
            return f"[error] {type(e).__name__}: {e}"

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = (msg.get("content") or "").strip()
        if (
            getattr(getattr(app, "backend", None), "remote", False)
            and effort is None
            and not think
        ):
            text = "[Codex uses its minimum supported reasoning effort]\n" + text if text else text
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
