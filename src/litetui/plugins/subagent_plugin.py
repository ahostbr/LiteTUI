"""Subagent tool: an independent one-shot completion on the active backend.

No tools, no parent history — the child sees only its own prompt (and an
optional system message). The token ceiling is the app's own thinking-safe budget, settings.compact_max_tokens
(the user 2026-09-08 21:0x: "litetui already has a think token budget set reuse
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
from copy import deepcopy
from pathlib import Path

from litetui import model_transport, settings_runtime, tool_schemas
from litetui import tasks as tasks_mod
from litetui.picker import pick
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


def _default_model(app):
    from litetui.subagent_routing import resolve_route

    route = resolve_route(app)
    if route['backend'] != getattr(getattr(app, 'backend', None), 'name', 'lmstudio'):
        raise model_transport.ProviderError(
            f"Subagent route to {route['backend']} requires cross-backend child transport.")
    return route['model']


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

    persisted = _default_model(app)
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
        explicit_model = (args.get("model") or "").strip() or None
        explicit_backend = (args.get("backend") or "").strip() or None
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
            from litetui.subagent_dispatch import complete_child
            data = complete_child(app, payload, explicit_model=explicit_model,
                                  explicit_backend=explicit_backend)
            model = data.get("model", explicit_model or getattr(app, "model_id", "?"))
        except Exception as e:  # noqa: BLE001 - tool boundary returns failures as visible results
            return f"[error] {type(e).__name__}: {e}"

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = (msg.get("content") or "").strip()
        if (
            data.get("backend") == "codex"
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


def _save_route(app, route, *, global_default=False):
    from litetui.settings_service import SettingChange
    from litetui.subagent_routing import validate_route

    validate_route(route, override=not global_default)
    directory = getattr(app, 'convo_dir', None)
    if not global_default and not directory:
        raise ValueError('Open a conversation before setting its subagent override.')
    service = settings_runtime.service_for(app)
    conversation_id = directory.name if directory else '__defaults__'
    snapshot = service.snapshot(conversation_id)
    key = 'subagent_route' if global_default else 'subagent_route_override'
    scope = 'device' if global_default else 'conversation'
    changes = [SettingChange(key, route, scope)]
    candidate = deepcopy(app.settings)
    setattr(candidate, key, route)
    if not global_default:
        # Choosing inherit/default retires the old model-only override too.
        changes.append(SettingChange('subagent_model', None, 'conversation'))
        candidate.subagent_model = None
    result = service.save_patch(conversation_id, changes, snapshot.revisions)
    if not result.fully_saved:
        raise OSError('; '.join(p.error for p in result.persistence if not p.saved))
    settings_runtime.apply_saved_result(app, candidate, result)


def _parse_route(app, arg, *, global_default=False):
    from litetui.subagent_routing import validate_route

    if arg.lower() == 'inherit' and not global_default:
        return None
    if arg.lower() == 'default':
        return None if global_default else {}
    parts = arg.split(maxsplit=1)
    route = {'backend': parts[0], 'model': parts[1]} if len(parts) == 2 else {
        'backend': app.backend.name, 'model': arg}
    validate_route(route)
    if route['backend'] == app.backend.name:
        catalog = getattr(app.backend, 'models', {})
        if catalog and route['model'] not in catalog:
            raise ValueError(f"{route['backend']} model not available: {route['model']}")
    return route


def _cmd_route(app, arg, *, global_default=False):
    backend = app.backend
    directory = getattr(app, 'convo_dir', None)

    def selected(value):
        if value is None:
            return
        if app.backend is not backend or getattr(app, 'convo_dir', None) != directory:
            app.system_message('Conversation changed; open the subagent picker again.')
            return
        try:
            route = _parse_route(app, value, global_default=global_default)
            _save_route(app, route, global_default=global_default)
        except (OSError, ValueError) as exc:
            app.system_message(f'Subagent route was not saved: {exc}')
            return
        scope = 'Global' if global_default else 'Conversation override'
        label = f"{route['backend']} / {route['model']}" if route else (
            'Inherit global' if route is None and not global_default else 'Follow parent')
        app.system_message(f'{scope} subagent route: {label}. Parent unchanged.')

    if arg.strip():
        selected(arg.strip())
        return
    rows = [('default', 'Follow parent backend and model')]
    if not global_default:
        rows.insert(0, ('inherit', 'Inherit global subagent route'))
    rows.extend((f'{backend.name} {key}', key) for key in getattr(backend, 'models', {}))
    pick(app, 'Subagent route · ' + ('global' if global_default else 'this conversation'),
         rows, selected)


def _cmd_subagent_set(app, name, arg):
    _cmd_route(app, arg)


def _cmd_subagent_global(app, name, arg):
    _cmd_route(app, arg, global_default=True)


def _register(ctx) -> None:
    ctx.tool(SPEC, _make_runner(ctx.app), policy=NETWORK_READ_POLICY)
    ctx.command(('/subagent-set',), _cmd_subagent_set,
                palette='Conversation subagent override', group='model', order=25,
                help='This conversation only: [backend] model overrides global; default follows parent; inherit uses global.')
    ctx.command(('/subagent-global',), _cmd_subagent_global,
                palette='Global subagent route', group='model', order=26,
                help='All instances: backend model selects route; default follows each parent.')


PLUGIN = PluginManifest(id="subagent", register=_register)
