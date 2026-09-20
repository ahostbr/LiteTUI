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

@pytest.mark.asyncio
async def test_second_cancellation_cannot_interrupt_cleanup_and_persistence(tmp_path):
    import asyncio
    from litetui.agent_supervisor import finish_child
    process = AgentProcess()
    process.conversation_id = 'actual-convo'
    collecting, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def collect(**kwargs):
        collecting.set()
        await asyncio.Event().wait()
    async def close(**kwargs):
        closing.set()
        await release.wait()
        return True
    process.collect_turn, process.close = collect, close
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    notices = []
    task = asyncio.create_task(finish_child(process, inbox, parent='parent', child_id='child',
        branch=None, evidence=[], notify=notices.append))
    await collecting.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    pending = inbox.pending('parent')
    assert len(pending) == 1
    assert pending[0]['result']['cleanup']['state'] == 'confirmed'
    assert not notices
@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['disk', 'notify'])
async def test_delivery_fault_does_not_skip_owned_cleanup(tmp_path, fault):
    from litetui.agent_supervisor import finish_child
    process = AgentProcess()
    process.conversation_id = 'actual-convo'
    closed = []
    async def collect(**kwargs): return {'status': 'completed', 'summary': 'done'}
    async def close(**kwargs):
        closed.append(True)
        return True
    process.collect_turn, process.close = collect, close
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    notices = []
    def notify(event):
        notices.append(event)
        raise OSError('notification unavailable')
    if fault == 'disk':
        def persist(*args): raise OSError('disk unavailable')
        inbox.persist = persist
    with pytest.raises(OSError):
        await finish_child(process, inbox, parent='parent', child_id='child', branch=None,
                           evidence=[], notify=notify)
    assert closed == [True]
    pending = AgentInbox(tmp_path / 'inbox.sqlite').pending('parent')
    assert len(pending) == (1 if fault == 'notify' else 0)
    assert len(notices) == (1 if fault == 'notify' else 0)
@pytest.mark.asyncio
@pytest.mark.parametrize('materialized', [True, False])
async def test_durable_completion_requires_materialized_storage_when_requested(tmp_path, materialized):
    from litetui.agent_supervisor import finish_child
    process = AgentProcess()
    process.conversation_id = 'actual-convo'
    async def collect(**kwargs): return {'status': 'completed', 'summary': 'done'}
    async def close(**kwargs): return True
    process.collect_turn, process.close = collect, close
    if materialized:
        directory = tmp_path / '.convos' / 'actual-convo'
        directory.mkdir(parents=True)
        (directory / 'convo.jsonl').write_text('{}\n', encoding='utf-8')
        (directory / 'settings.json').write_text('{}', encoding='utf-8')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    completion = await finish_child(process, inbox, parent='parent', child_id='child',
        branch=None, evidence=[], notify=lambda event: None, data_root=tmp_path)
    result = inbox.get('parent', completion)
    assert result['status'] == ('completed' if materialized else 'failed')
    if materialized:
        assert result['storage']['transcript'] == str((directory / 'convo.jsonl').resolve())
    else:
        assert 'transcript' in result['storage_error']