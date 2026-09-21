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
        branch=None, evidence=[], supported_levels=[], notify=notify,
        parent_conversation='original-chat')
    assert AgentRegistry(tmp_path / 'registry.sqlite').parent_conversation('parent', 'child') == 'original-chat'
    assert registry.parent_conversation('other', 'child') is None
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

@pytest.mark.asyncio
async def test_runtime_cancel_keeps_durable_result_and_reconcilable_claim(tmp_path):
    import asyncio
    from litetui.agent_runtime import run_prepared_child
    from litetui.agent_registry import AgentRegistry
    from litetui.agent_inbox import AgentInbox
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    entered = asyncio.Event()
    class Process:
        conversation_id = 'convo'
        async def start_python(self, **kwargs): pass
        async def rpc_handshake(self, spec, **kwargs):
            return {'conversation_id': 'convo', 'pid': 123, 'process_created': 'stamp'}
        async def send_prompt(self, text): pass
        async def collect_turn(self, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        async def close(self): return True
    spec = validate_request({'prompt': 'task', 'backend': 'codex', 'model': 'model',
                             'workspace': str(tmp_path)}, parent_profile='autonomous', depth=0)
    notices = []
    task = asyncio.create_task(run_prepared_child(spec, Process(), registry=registry, inbox=inbox,
        parent='parent', child_id='child', workspace=tmp_path, data_root=tmp_path,
        branch=None, evidence=[], supported_levels=[], notify=notices.append))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    pending = inbox.pending('parent')
    assert len(pending) == 1
    assert pending[0]['result']['status'] == 'cancelled'
    assert not registry.active('parent')
    # Explicit reconciliation remains idempotent after cancel already settles.
    assert registry.reconcile('parent', inbox=inbox) == []
    assert not notices