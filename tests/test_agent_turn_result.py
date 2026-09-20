import pytest
from litetui.agent_supervisor import AgentProcess
from litetui.agent_launcher import LaunchBlocked


@pytest.mark.asyncio
async def test_collects_only_completed_turn_as_success():
    process = AgentProcess()
    events = iter([{'type':'response','ok':True}, {'type':'turn_start'},
                   {'type':'text_delta','text':'hello'}, {'type':'text_delta','text':' world'},
                   {'type':'turn_end','stopReason':'stop'}])
    async def receive(**kwargs): return next(events)
    process.receive = receive
    result = await process.collect_turn(timeout=1)
    assert result == {'status':'completed','summary':'hello world','stop_reason':'stop'}


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [{'type':'response','ok':False,'error':'refused'},
                                  {'type':'turn_end','stopReason':'stop'},
                                  {'type':'error','error':'failed'}])
async def test_rejection_or_unstarted_completion_is_not_success(event):
    process = AgentProcess()
    async def receive(**kwargs): return event
    process.receive = receive
    with pytest.raises(LaunchBlocked):
        await process.collect_turn(timeout=.1)


@pytest.mark.asyncio
async def test_timeout_is_bounded_not_completed():
    import asyncio
    process = AgentProcess()
    async def receive(**kwargs): await asyncio.Event().wait()
    process.receive = receive
    with pytest.raises(LaunchBlocked, match='timed out'):
        await process.collect_turn(timeout=.02)

@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['tool_approval_requested', 'user_input_requested', 'model_load_requested'])
async def test_real_human_request_events_fail_without_a_relay(kind):
    process = AgentProcess()
    events = iter([{'type': kind}, {'type': 'error', 'error': 'request was ignored'}])
    async def receive(**kwargs): return next(events)
    process.receive = receive
    with pytest.raises(LaunchBlocked, match='human relay'):
        await process.collect_turn(timeout=1)

@pytest.mark.asyncio
@pytest.mark.parametrize('detail', ['backend connection lost', None])
async def test_failed_end_retains_diagnostic_even_without_text(detail):
    process = AgentProcess()
    events = iter([{'type': 'turn_start'},
                   {'type': 'turn_end', 'stopReason': 'error', 'error': detail}])
    async def receive(**kwargs): return next(events)
    process.receive = receive
    result = await process.collect_turn(timeout=1)
    assert result['status'] == 'failed'
    assert result['summary'] == (detail or 'Child turn ended: error')
    assert result['error'] == (detail or 'Child turn ended: error')