"""Validated agent launch contracts and contained headless startup."""
from dataclasses import dataclass
import hmac


class LaunchBlocked(ValueError):
    pass


@dataclass(frozen=True)
class LaunchSpec:
    prompt: str
    backend: str
    model: str
    workspace: str
    tool_profile: str
    headed: bool = False
    workspace_mode: str = 'worktree'
    reasoning_effort: str | None = None
    thinking_level: str | None = None
    child_depth: int = 1
    launch: dict | None = None


def validate_request(request, *, parent_profile, depth):
    if not isinstance(request, dict):
        raise LaunchBlocked('Launch request must be an object')
    allowed = {'prompt', 'backend', 'model', 'workspace', 'tool_profile', 'headed',
               'workspace_mode', 'reasoning_effort', 'thinking_level', 'launch'}
    if set(request) - allowed:
        raise LaunchBlocked('Unknown or runtime-owned launch fields')
    for key in ('prompt', 'model', 'workspace', 'backend'):
        if not isinstance(request.get(key), str) or not request[key].strip() or '\x00' in request[key]:
            raise LaunchBlocked(f'Explicit {key} is required')
    from litetui.llm_backend import BACKEND_NAMES
    if request['backend'] not in BACKEND_NAMES:
        raise LaunchBlocked('Unsupported backend; fallback is forbidden')
    if type(depth) is not int or depth != 0:
        raise LaunchBlocked('Child spawn depth budget exhausted')
    if parent_profile not in ('autonomous', 'interactive', 'scheduled'):
        raise LaunchBlocked('Unknown parent policy')
    profile = request.get('tool_profile', parent_profile)
    # Profiles are not a simple numeric hierarchy: scheduled and interactive
    # have different approval semantics. Without explicit delegation, require
    # the same profile rather than guessing that one is universally weaker.
    if profile != parent_profile:
        raise LaunchBlocked('Child policy must match parent delegation')
    headed = request.get('headed', False)
    if type(headed) is not bool:
        raise LaunchBlocked('headed must be a boolean')
    mode = request.get('workspace_mode', 'worktree')
    if mode not in ('worktree', 'explicit'):
        raise LaunchBlocked('Unsupported workspace mode')
    effort, thinking = request.get('reasoning_effort'), request.get('thinking_level')
    if effort is not None and thinking is not None:
        raise LaunchBlocked('Specify reasoning_effort or thinking_level, not both')
    for value in (effort, thinking):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise LaunchBlocked('Invalid thinking selection')
    launch = request.get('launch')
    if launch is not None:
        from dataclasses import fields

        from litetui.launch_options import LaunchOptions
        if not isinstance(launch, dict) or set(launch) - {f.name for f in fields(LaunchOptions)}:
            raise LaunchBlocked('Invalid launch options')
    return LaunchSpec(request['prompt'], request['backend'], request['model'], request['workspace'],
                      profile, headed, mode, effort, thinking, launch=launch)


def validate_handshake(spec, event, *, child_id, conversation_id, token, workspace):
    if not isinstance(event, dict) or event.get('type') != 'agent_ready' or event.get('status') != 'ready':
        raise LaunchBlocked('Child has not reported effective readiness')
    received = event.get('token')
    if not isinstance(received, str) or not hmac.compare_digest(received, token):
        raise LaunchBlocked('Untrusted child handshake')
    expected = {'child_id': child_id, 'conversation_id': conversation_id, 'workspace': workspace,
                'backend': spec.backend, 'model': spec.model, 'tool_profile': spec.tool_profile,
                'reasoning_effort': spec.reasoning_effort, 'thinking_level': spec.thinking_level}
    for key, value in expected.items():
        if event.get(key) != value:
            raise LaunchBlocked(f'Child effective {key} does not match launch request')
    if type(event.get('pid')) is not int or event['pid'] <= 0:
        raise LaunchBlocked('Child process identity missing')
    if not isinstance(event.get('process_created'), str) or not event['process_created']:
        raise LaunchBlocked('Child process creation identity missing')
    return True


def validate_capabilities(spec, supported_levels):
    """Require caller-probed metadata for an explicit thinking selection."""
    requested = spec.reasoning_effort if spec.reasoning_effort is not None else spec.thinking_level
    if requested is None:
        return True
    if (not isinstance(supported_levels, (list, tuple))
            or any(not isinstance(level, str) for level in supported_levels)
            or requested not in supported_levels):
        raise LaunchBlocked('Requested thinking selection is not supported by measured provider capabilities')
    return True


def validate_process_identity(event, *, owned_pid, probe=None):
    """Never trust an unverified self-reported PID, even with a valid nonce."""
    if probe is None:
        from litetui.task_supervisor import process_creation_identity
        probe = process_creation_identity
    if not isinstance(event, dict) or type(event.get('pid')) is not int or event['pid'] != owned_pid:
        raise LaunchBlocked('Child identity differs from owned process')
    current = probe(owned_pid)
    if not current or current != event.get('process_created'):
        raise LaunchBlocked('Child identity unknown or PID reused')
    return True

async def start_headless_child(spec, process, *, workspace, data_root, supported_levels, on_ready=None):
    """Wire validated hosted invocation to contained startup and authenticated RPC.

    Internal only: caller must prepare isolated workspace, persistent storage,
    registry and concurrency ownership. Local loading is explicit in launch
    options. Headed transport remains separate (public CLI). No prompt on argv.
    """
    from pathlib import Path
    from litetui.agent_ancestry import require_root_launcher
    require_root_launcher()
    if type(spec.child_depth) is not int or spec.child_depth != 1:
        raise LaunchBlocked('Invalid managed child depth')
    if spec.headed:
        raise LaunchBlocked('Headed child transport is not integrated')
    validate_capabilities(spec, supported_levels)
    target = Path(workspace).resolve()
    root = Path(data_root).resolve()
    if not target.is_dir() or not root.is_dir():
        raise LaunchBlocked('Prepared workspace and persistent data root required')
    args = ['--rpc', '--backend', spec.backend, '--model', spec.model,
            '--tool-profile', spec.tool_profile]
    if spec.reasoning_effort is not None:
        args += ['--reasoning-effort', spec.reasoning_effort]
    elif spec.thinking_level is not None:
        args += ['--thinking-level', spec.thinking_level]
    if spec.launch:
        from litetui.launch_options import LaunchOptions, to_argv
        from litetui.settings import load
        options = LaunchOptions(**spec.launch)
        try:
            options.overrides(load(root), spec.backend, spec.model)
        except ValueError as exc:
            raise LaunchBlocked(str(exc)) from exc
        args += to_argv(options)
    try:
        await process.start_python(module='litetui.cli', args=args, cwd=target,
                                   env={'LITETUI_DATA_ROOT': str(root),
                                        'LITETUI_AGENT_DEPTH': str(spec.child_depth)})
        ready = await process.rpc_handshake(spec, workspace=str(target))
        if on_ready is not None:
            on_ready(ready)
        await process.send_prompt(spec.prompt)
        return ready
    except BaseException as original:
        import asyncio
        cancelled = original if isinstance(original, asyncio.CancelledError) else None
        async def close_bounded():
            async with asyncio.timeout(10):
                return await process.close()
        cleanup = asyncio.create_task(close_bounded())
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError as exc:
                if cleanup.cancelled():
                    break
                cancelled = cancelled or exc
            except Exception:
                # Cleanup failure must not replace the original launch exception,
                # especially cancellation. Bound launches persist an unconfirmed
                # outcome through the runtime's subsequent cleanup attempt.
                break
        if cancelled is not None:
            raise cancelled
        raise
