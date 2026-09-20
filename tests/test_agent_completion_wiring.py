import pytest
from litetui.agent_supervisor import AgentProcess
from litetui.agent_inbox import AgentInbox


@pytest.mark.asyncio
async def test_cleanup_then_persist_then_notify_with_actual_conversation(tmp_path):
    from litetui.agent_supervisor import finish_child
    process = AgentProcess()
    process.conversation_id = 'actual-convo'
    calls = []
    async def collect(**kwargs):
        return {'status':'completed','summary':'done','stop_reason':'stop'}
    async def close(**kwargs):
        calls.append('closed')
        return True
    process.collect_turn = collect
    process.close = close
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    def notify(event):
        assert calls == ['closed']
        assert inbox.get('parent', event['completion_id'])['conversation_id'] == 'actual-convo'
        calls.append('notified')
    completion = await finish_child(process, inbox, parent='parent', child_id='child',
        branch='fixture', evidence=['trace.json'], notify=notify)
    assert inbox.get('parent', completion)['cleanup']['state'] == 'confirmed'
    assert calls == ['closed', 'notified']


@pytest.mark.asyncio
async def test_child_disconnect_records_failure_not_success(tmp_path):
    from litetui.agent_supervisor import finish_child
    from litetui.agent_launcher import LaunchBlocked
    process = AgentProcess()
    process.conversation_id = 'actual-convo'
    async def failed(**kwargs): raise LaunchBlocked('disconnected')
    async def close(**kwargs): return False
    process.collect_turn = failed
    process.close = close
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    completion = await finish_child(process, inbox, parent='parent', child_id='child',
        branch='fixture', evidence=[], notify=lambda event: None)
    result = inbox.get('parent', completion)
    assert result['status'] == 'failed'
    assert 'disconnected' in result['summary']
    assert result['cleanup']['state'] == 'unconfirmed'

@pytest.mark.asyncio
async def test_cancelled_collection_persists_replayable_cancelled_outcome(tmp_path):
    import asyncio
    from litetui.agent_supervisor import finish_child
    process = AgentProcess()
    process.conversation_id = 'actual-convo'
    entered = asyncio.Event()
    async def wait(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    async def close(**kwargs): return True
    process.collect_turn = wait
    process.close = close
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    notices = []
    task = asyncio.create_task(finish_child(process, inbox, parent='parent', child_id='child',
        branch=None, evidence=[], notify=notices.append))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not notices
    pending = AgentInbox(tmp_path / 'inbox.sqlite').pending('parent')
    assert len(pending) == 1
    assert pending[0]['result']['status'] == 'cancelled'
