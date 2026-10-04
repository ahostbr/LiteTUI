"""Independent one-shot child sessions: route, provider config, auth and cleanup.

No parent client, backend, tools, history or invocation settings are inherited.
The host subagent remains a prompt-only completion, not a tool-using spawned seat.
"""
from __future__ import annotations

import asyncio
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from litetui import model_transport, sanitize
from litetui.llm_backend import BackendError, make_backend
from litetui.settings import load
from litetui.settings_runtime import service_for
from litetui.subagent_routing import LOCAL_BACKENDS, resolve_route

CHILD_TIMEOUT = 600
CLEANUP_TIMEOUT = 30


def _safe_error(error):
    if isinstance(error, BackendError):
        text = sanitize.redact_secrets(sanitize.strip_escapes(str(error)))
        return " ".join(text.split())[:500]
    if isinstance(error, TimeoutError):
        return "Child deadline exceeded; retry or check provider availability"
    return f"Request failed ({type(error).__name__}); check the selected provider auth/configuration"


class ChildError(model_transport.ProviderError):
    """Visible refusal or child failure, never a silent provider fallback."""


async def _close(backend):
    errors = []
    resources = [backend, getattr(backend, 'app_server', None)]
    for resource in resources:
        close = getattr(resource, 'close', None)
        if close is None:
            continue
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT):
                await close()
        except Exception as exc:  # noqa: BLE001 - close every owned resource before reporting
            errors.append(_safe_error(exc))
    if errors:
        raise ChildError('; '.join(errors))


async def _execute(backend, payload):
    if getattr(backend, 'owns_native_turns', False):
        return await _claude(backend, payload)
    if getattr(backend, 'name', None) == 'codex':
        # Existing Codex sidecall creates/owns its own transport and preserves
        # the requested child-only effort contract. Run outside this loop.
        return await asyncio.to_thread(model_transport.complete_sidecall,
                                       SimpleNamespace(backend=backend), payload)
    return await _openai(backend, payload)


async def _openai(backend, payload):
    from openai import AsyncOpenAI

    request = dict(payload)
    request.pop('chat_template_kwargs', None)
    effort = request.pop('reasoning_effort', None)
    if effort not in (None, 'none'):
        levels = backend.reasoning_levels(request['model'])
        if effort not in levels:
            raise ChildError(f'{backend.name} does not support child reasoning effort {effort!r}')
        request['reasoning_effort'] = effort
    # ClinePass refreshes its own credentials at request time. FreeBackend's
    # HTTP client owns the FreeRouter and cannot route into a paid endpoint.
    key = getattr(backend, 'api_key_provider', None)
    if key is None:
        provider = getattr(backend, 'api_key', None)
        key = provider() if callable(provider) else 'litetui'
    options = {'base_url': backend.base_url(), 'api_key': key, 'max_retries': 0, 'timeout': 600}
    http_client = getattr(backend, 'http_client', None)
    if callable(http_client):
        options['http_client'] = http_client()
    async with AsyncOpenAI(**options) as client:
        try:
            result = await client.chat.completions.create(**request)
        except Exception as exc:
            sentence = getattr(backend, 'error_sentence', lambda _: None)(exc)
            if sentence:
                raise ChildError(sentence) from exc
            raise
    return result.model_dump()


async def _claude(backend, payload):
    from litetui.claude_session import ClaudeSession

    messages = payload.get('messages', [])
    system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system')
    prompt = '\n\n'.join(m['content'] for m in messages if m['role'] == 'user')
    effort = payload.get('reasoning_effort')
    options = {'model': payload['model'], 'tools': [], 'allowed_tools': [],
               'permission_mode': 'dontAsk', 'max_turns': 1}
    if system:
        options['system_prompt'] = system
    if effort not in (None, 'none'):
        if effort not in backend.reasoning_levels(payload['model']):
            raise ChildError(f'Claude does not support child reasoning effort {effort!r}')
        options['effort'] = effort
    elif effort == 'none':
        levels = backend.reasoning_levels(payload['model'])
        if levels:
            options['effort'] = levels[0]
    with tempfile.TemporaryDirectory(prefix='litetui-claude-child-') as cwd:
        sdk_options = await backend._options(cwd=cwd, **options)
        if payload.get('max_tokens') is not None:
            # Supported Claude Code child-process setting, not a process-wide
            # environment mutation. Preserve the backend's cache/safety env.
            sdk_options.env = {**sdk_options.env,
                               'CLAUDE_CODE_MAX_OUTPUT_TOKENS': str(payload['max_tokens'])}
        session = ClaudeSession(sdk_options)
        text, usage = [], {}
        completed = False
        failure = None
        try:
            async with asyncio.timeout(CHILD_TIMEOUT):
                await session.start()
                await session.query(str(uuid4()), prompt)
                async for message in session.events():
                    kind = type(message).__name__
                    if kind == 'AssistantMessage':
                        for block in message.content:
                            if type(block).__name__ == 'TextBlock':
                                text.append(block.text)
                    elif kind == 'ResultMessage':
                        if getattr(message, 'is_error', False):
                            raise ChildError('Claude child reported an unsuccessful result; check Claude auth/provider availability')
                        completed = True
                        usage = getattr(message, 'usage', None) or {}
                        if not text and getattr(message, 'result', None):
                            text.append(message.result)
                if not completed:
                    raise ChildError('Claude child stream ended without a terminal result')
        except Exception as exc:
            failure = exc
            raise
        finally:
            # ClaudeSession owns its bounded subprocess cleanup/escalation.
            try:
                await session.close()
                if session.lifecycle.cleanup_errors:
                    raise ChildError('an owned session could not close cleanly')
            except Exception as exc:
                detail = f'; initial failure: {_safe_error(failure)}' if failure else ''
                raise ChildError(f'Claude child cleanup failed: {_safe_error(exc)}{detail}') from exc
    return {'choices': [{'message': {'content': '\n'.join(text), 'reasoning_content': ''}}],
            'usage': {'completion_tokens': usage.get('output_tokens')}}


def complete_child(app, payload, *, explicit_model=None, explicit_backend=None):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise ChildError('Run synchronous child dispatch in a tool worker thread, not the UI event loop')
    route = resolve_route(app, explicit_model)
    if explicit_backend:
        from litetui.subagent_routing import validate_route
        route = {'backend': explicit_backend, 'model': explicit_model or route['model']}
        validate_route(route)
    settings = load(service_for(app).root)
    settings.backend = route['backend']
    settings.default_model = route['model']
    # Factory hooks are app-global and can mutate a parent on model loads. Child
    # dispatch never calls load; fresh settings never carry invocation overrides.
    try:
        backend = make_backend(settings)
    except Exception as exc:
        raise ChildError(f"{route['backend']} subagent configuration: {_safe_error(exc)}") from exc
    request = dict(payload, model=route['model'])

    async def run():
        failure = None
        try:
            if route['backend'] in LOCAL_BACKENDS:
                from litetui.subagent_local import admit_local
                try:
                    # Refresh the flag too: another window can revoke it.
                    _, enabled = service_for(app).global_value('allow_local_subagents')
                    await admit_local(backend, route['model'], enabled)
                    if route['backend'] == 'lmstudio':
                        try:
                            model_transport._refuse_unsupported_local_lm(backend)
                        except Exception as exc:
                            raise ChildError('LM Studio local subagents are unsupported until the usage/lease protocol lands; a resident check cannot prevent request-time JIT loading after eviction') from exc
                except ValueError as exc:
                    raise ChildError(str(exc)) from exc
                # Never call ensure_chat_ready on a local child: some providers
                # implement it by loading, which would undo admission above.
            else:
                ready = getattr(backend, 'ensure_chat_ready', None)
                if ready:
                    await ready(route['model'])
                else:
                    await backend.ensure_running()
            result = await _execute(backend, request)
            return dict(result, backend=route['backend'], model=route['model'])
        except Exception as exc:
            failure = exc
            raise ChildError(f"{route['backend']} subagent: {_safe_error(exc)}") from exc
        finally:
            try:
                await _close(backend)
            except Exception as exc:
                detail = f'; initial failure: {_safe_error(failure)}' if failure else ''
                raise ChildError(f"{route['backend']} subagent cleanup: {_safe_error(exc)}{detail}") from exc

    return asyncio.run(run())
