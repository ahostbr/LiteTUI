from concurrent.futures import ThreadPoolExecutor
import pytest
from litetui.agent_launcher import LaunchBlocked


def test_claim_survives_reopen_and_budget_is_not_per_connection(tmp_path):
    from litetui.agent_registry import AgentRegistry
    path = tmp_path / 'registry.sqlite'
    AgentRegistry(path).claim('parent', 'child-a', limit=1)
    with pytest.raises(LaunchBlocked, match='budget'):
        AgentRegistry(path).claim('parent', 'child-b', limit=1)
    assert AgentRegistry(path).active('parent')[0]['child_id'] == 'child-a'


def test_parallel_claims_cannot_overbook(tmp_path):
    from litetui.agent_registry import AgentRegistry
    path = tmp_path / 'registry.sqlite'
    AgentRegistry(path)
    def claim(i):
        try:
            AgentRegistry(path).claim('parent', f'child-{i}', limit=1)
            return True
        except LaunchBlocked:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(4))) == 1


def test_terminal_requires_confirmed_cleanup_and_completion(tmp_path):
    from litetui.agent_registry import AgentRegistry
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    registry.claim('parent', 'child', limit=1)
    with pytest.raises(LaunchBlocked):
        registry.settle('parent', 'child', completion_id=None, cleanup_confirmed=True)
    with pytest.raises(LaunchBlocked):
        registry.settle('parent', 'child', completion_id='completion', cleanup_confirmed=False)
    assert len(registry.active('parent')) == 1
    registry.settle('parent', 'child', completion_id='completion', cleanup_confirmed=True)
    assert not registry.active('parent')
    registry.claim('parent', 'child-next', limit=1)


def test_identity_binding_and_parent_scope(tmp_path):
    from litetui.agent_registry import AgentRegistry
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    registry.claim('parent', 'child', limit=1)
    with pytest.raises(LaunchBlocked):
        registry.bind('other', 'child', conversation_id='convo', pid=123, created='stamp')
    registry.bind('parent', 'child', conversation_id='convo', pid=123, created='stamp')
    row = registry.active('parent')[0]
    assert (row['conversation_id'], row['pid'], row['process_created']) == ('convo', 123, 'stamp')
    with pytest.raises(LaunchBlocked):
        registry.bind('parent', 'child', conversation_id='wrong', pid=124, created='other')


def test_recovery_observation_never_releases_unknown_process(tmp_path):
    from litetui.agent_registry import AgentRegistry
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    registry.claim('parent', 'child', limit=1)
    registry.bind('parent', 'child', conversation_id='convo', pid=123, created='stamp')
    for stamp, expected in [(None, 'unknown'), ('different', 'mismatch'), ('stamp', 'matching')]:
        rows = registry.observe('parent', probe=lambda pid: stamp)
        assert rows[0]['identity_state'] == expected
        assert len(registry.active('parent')) == 1


def test_budget_is_shared_across_parents_and_settlement_is_scoped(tmp_path):
    from litetui.agent_registry import AgentRegistry
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    registry.claim('parent-a', 'child-a', limit=1)
    with pytest.raises(LaunchBlocked, match='budget'):
        registry.claim('parent-b', 'child-b', limit=1)
    with pytest.raises(LaunchBlocked):
        registry.settle('parent-b', 'child-a', completion_id='completion', cleanup_confirmed=True)
    assert len(registry.active('parent-a')) == 1
    registry.settle('parent-a', 'child-a', completion_id='completion', cleanup_confirmed=True)
    registry.settle('parent-a', 'child-a', completion_id='completion', cleanup_confirmed=True)
    with pytest.raises(LaunchBlocked):
        registry.settle('parent-a', 'child-a', completion_id='conflict', cleanup_confirmed=True)
    registry.claim('parent-b', 'child-b', limit=1)