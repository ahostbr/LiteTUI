import pytest
from litetui.agent_launcher import validate_request


@pytest.mark.asyncio
async def test_runtime_claim_bind_persist_settle_before_notify(tmp_path):
    from litetui.agent_runtime import run_prepared_child
    from litetui.agent_registry import AgentRegistry
    from litetui.agent_inbox import AgentInbox
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    directory = tmp_path / '.convos' / 'convo'
    directory.mkdir(parents=True)
    (directory / 'convo.jsonl').write_text('{}\n')
    (directory / 'settings.json').write_text('{}')
    calls = []
    class Process:
        conversation_id = 'convo'
        async def start_python(self, **kwargs):
            assert registry.active('parent')[0]['state'] == 'claimed'
            calls.append('start')
        async def rpc_handshake(self, spec, **kwargs):
            return {'conversation_id': 'convo', 'pid': 123, 'process_created': 'stamp'}
        async def send_prompt(self, text):
            assert registry.active('parent')[0]['state'] == 'running'
            calls.append('prompt')
        async def collect_turn(self, **kwargs):
            return {'status': 'completed', 'summary': 'done'}
        async def close(self):
            calls.append('close')
            return True
    spec = validate_request({'prompt': 'task', 'backend': 'codex', 'model': 'model',
                             'workspace': str(tmp_path)}, parent_profile='autonomous', depth=0)
    def notify(event):
        assert inbox.get('parent', event['completion_id'])['summary'] == 'done'
        assert not registry.active('parent')
        calls.append('notify')
    ident = await run_prepared_child(spec, Process(), registry=registry, inbox=inbox,
        parent='parent', child_id='child', workspace=tmp_path, data_root=tmp_path,
        branch=None, evidence=[], supported_levels=[], notify=notify)
    assert inbox.get('parent', ident)['status'] == 'completed'
    assert calls == ['start', 'prompt', 'close', 'notify']


@pytest.mark.asyncio
async def test_unready_failure_retains_claim_instead_of_inventing_conversation(tmp_path):
    from litetui.agent_runtime import run_prepared_child
    from litetui.agent_registry import AgentRegistry
    from litetui.agent_inbox import AgentInbox
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    class Process:
        conversation_id = None
        async def start_python(self, **kwargs): raise OSError('failed startup')
        async def close(self): return True
    spec = validate_request({'prompt': 'task', 'backend': 'codex', 'model': 'model',
                             'workspace': str(tmp_path)}, parent_profile='autonomous', depth=0)
    with pytest.raises(OSError, match='failed startup'):
        await run_prepared_child(spec, Process(), registry=registry, inbox=inbox,
            parent='parent', child_id='child', workspace=tmp_path, data_root=tmp_path,
            branch=None, evidence=[], supported_levels=[], notify=lambda event: None)
    assert registry.active('parent')[0]['conversation_id'] is None
    assert not inbox.pending('parent')
