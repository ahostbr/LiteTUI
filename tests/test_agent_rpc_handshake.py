import pytest
from litetui.agent_launcher import validate_request, LaunchBlocked
from litetui.agent_supervisor import AgentProcess


@pytest.mark.asyncio
async def test_real_rpc_envelope_checks_effective_state_and_owned_pid(tmp_path):
    spec = validate_request({'prompt':'hi','backend':'codex','model':'model',
        'workspace':str(tmp_path),'reasoning_effort':'high'}, parent_profile='autonomous', depth=0)
    process = AgentProcess()
    from types import SimpleNamespace
    process.process = SimpleNamespace(pid=123, returncode=None)
    event = {'type':'ready','launch_status':'ready','backend':'codex','model':'model',
             'cwd':str(tmp_path),'tool_profile':'autonomous','thinking_level':'high',
             'pid':123,'process_created':'stamp'}
    async def receive(**kwargs): return event
    process.receive = receive
    async def close(**kwargs):
        process.ready = False
        return True
    process.close = close
    assert await process.rpc_handshake(spec, workspace=str(tmp_path), probe=lambda pid:'stamp') == event
    assert process.ready
    for key, value in [('model','fallback'),('thinking_level','low'),('launch_status','blocked'),('pid',999)]:
        original = event[key]
        event[key] = value
        with pytest.raises(LaunchBlocked):
            await process.rpc_handshake(spec, workspace=str(tmp_path), probe=lambda pid:'stamp')
        assert not process.ready
        event[key] = original
