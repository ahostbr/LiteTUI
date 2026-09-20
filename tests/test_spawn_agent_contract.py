import pytest


def request(**changes):
    return {'prompt': 'fix fixture', 'backend': 'codex', 'model': 'gpt-6-astra',
            'workspace': 'C:/fixture', **changes}


def test_defaults_headless_worktree_and_no_recursive_spawn():
    from litetui.agent_launcher import validate_request
    spec = validate_request(request(), parent_profile='autonomous', depth=0)
    assert spec.headed is False
    assert spec.workspace_mode == 'worktree'
    assert spec.tool_profile == 'autonomous'
    assert spec.child_depth == 1
    assert spec.model == 'gpt-6-astra'


@pytest.mark.parametrize('changes', [dict(headed='false'), dict(backend='unknown'), dict(model=''),
    dict(parent_conversation_id='forged'), dict(reasoning_effort='high', thinking_level='low'),
    dict(tool_profile='autonomous'), dict(workspace_mode='copy')])
def test_invalid_or_authority_increasing_request_is_rejected(changes):
    from litetui.agent_launcher import validate_request, LaunchBlocked
    with pytest.raises(LaunchBlocked):
        validate_request(request(**changes), parent_profile='scheduled', depth=0)


def test_child_cannot_spawn_grandchild_by_default():
    from litetui.agent_launcher import validate_request, LaunchBlocked
    with pytest.raises(LaunchBlocked, match='depth'):
        validate_request(request(), parent_profile='autonomous', depth=1)


def test_unknown_parent_authority_fails_closed():
    from litetui.agent_launcher import validate_request, LaunchBlocked
    with pytest.raises(LaunchBlocked, match='policy'):
        validate_request(request(), parent_profile=None, depth=0)


def test_handshake_checks_nonce_and_actual_effective_configuration():
    from litetui.agent_launcher import validate_request, validate_handshake, LaunchBlocked
    spec = validate_request(request(), parent_profile='autonomous', depth=0)
    expected = dict(child_id='child', conversation_id='convo', token='secret', workspace='C:/fixture')
    event = {'type': 'agent_ready', **expected, 'backend': 'codex', 'model': 'gpt-6-astra',
             'tool_profile': 'autonomous', 'reasoning_effort': None, 'thinking_level': None,
             'pid': 123, 'process_created': 'stamp', 'status': 'ready'}
    assert validate_handshake(spec, event, **expected) is True
    for field, value in [('token', 'forged'), ('backend', 'ninfer'), ('model', 'fallback'),
                         ('conversation_id', 'wrong'), ('pid', True), ('process_created', ''),
                         ('tool_profile', 'scheduled'), ('status', 'connecting')]:
        with pytest.raises(LaunchBlocked):
            validate_handshake(spec, {**event, field: value}, **expected)


def test_requested_thinking_requires_measured_provider_capability():
    from litetui.agent_launcher import validate_request, validate_capabilities, LaunchBlocked
    spec = validate_request(request(reasoning_effort='high'), parent_profile='autonomous', depth=0)
    for levels in (None, [], ['low']):
        with pytest.raises(LaunchBlocked, match='thinking'):
            validate_capabilities(spec, levels)
    assert validate_capabilities(spec, ['low', 'high']) is True


def test_default_thinking_does_not_invent_provider_capabilities():
    from litetui.agent_launcher import validate_request, validate_capabilities
    spec = validate_request(request(), parent_profile='autonomous', depth=0)
    assert validate_capabilities(spec, None) is True
