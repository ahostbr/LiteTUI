import pytest
from litetui.agent_runtime import run_prepared_child
from litetui.agent_registry import AgentRegistry
from litetui.agent_launcher import LaunchBlocked


@pytest.mark.asyncio
async def test_exhausted_budget_prevents_preparation(tmp_path):
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    registry.claim('other-parent', 'existing', limit=1)
    called = []
    with pytest.raises(LaunchBlocked, match='budget'):
        await run_prepared_child(None, None, registry=registry, inbox=None,
            parent='parent', child_id='new', workspace=None, data_root=None,
            branch=None, evidence=[], supported_levels=[], notify=None,
            prepare=lambda: called.append('prepared'))
    assert called == []
    assert not registry.active('parent')


@pytest.mark.asyncio
async def test_preparation_runs_after_claim_and_failure_retains_it(tmp_path):
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    def prepare():
        assert registry.active('parent')[0]['child_id'] == 'new'
        raise OSError('worktree interrupted')
    with pytest.raises(OSError, match='interrupted'):
        await run_prepared_child(None, None, registry=registry, inbox=None,
            parent='parent', child_id='new', workspace=None, data_root=None,
            branch=None, evidence=[], supported_levels=[], notify=None,
            parent_conversation='chat', prepare=prepare)
    reopened = AgentRegistry(tmp_path / 'registry.sqlite')
    assert reopened.active('parent')[0]['state'] == 'claimed'
    assert reopened.parent_conversation('parent', 'new') == 'chat'
