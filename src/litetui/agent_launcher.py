"""Full-agent launch contracts. Process startup is deliberately not enabled yet."""
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


def validate_request(request, *, parent_profile, depth):
    if not isinstance(request, dict):
        raise LaunchBlocked('Launch request must be an object')
    allowed = {'prompt', 'backend', 'model', 'workspace', 'tool_profile', 'headed',
               'workspace_mode', 'reasoning_effort', 'thinking_level'}
    if set(request) - allowed:
        raise LaunchBlocked('Unknown or runtime-owned launch fields')
    for key in ('prompt', 'model', 'workspace', 'backend'):
        if not isinstance(request.get(key), str) or not request[key].strip() or '\x00' in request[key]:
            raise LaunchBlocked(f'Explicit {key} is required')
    if request['backend'] not in ('codex', 'lmstudio', 'llamacpp', 'ninfer'):
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
    return LaunchSpec(request['prompt'], request['backend'], request['model'], request['workspace'],
                      profile, headed, mode, effort, thinking)


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
