import asyncio
import json
import sys
from pathlib import Path
import pytest
from litetui.agent_launcher import validate_request, LaunchBlocked


def spec(tmp_path):
    return validate_request({'prompt': 'do not execute before ready', 'backend': 'codex',
        'model': 'fixture', 'workspace': str(tmp_path)}, parent_profile='autonomous', depth=0)


def child(tmp_path, *, model='fixture', silent=False):
    path = tmp_path / 'child with spaces.py'
    path.write_text('''import json, os, sys, time
from litetui.task_supervisor import process_creation_identity
if os.environ.get('SILENT') == '1':
    time.sleep(30)
else:
    print(json.dumps({'type':'agent_ready', 'status':'ready', 'child_id':'child',
      'conversation_id':'convo', 'token':os.environ['CHILD_TOKEN'],
      'workspace':os.getcwd(), 'backend':'codex', 'model':os.environ['CHILD_MODEL'],
      'tool_profile':'autonomous', 'reasoning_effort':None, 'thinking_level':None,
      'pid':os.getpid(), 'process_created':process_creation_identity(os.getpid())}), flush=True)
    line = sys.stdin.readline()
    if line:
        print(json.dumps({'type':'accepted', 'command':json.loads(line)}), flush=True)
    sys.stdin.read()
''')
    # Use the actual interpreter, not Windows venv redirector (which has a
    # different PID). Production launcher must resolve that distinction too.
    return [getattr(sys, '_base_executable', sys.executable), str(path)], {'CHILD_MODEL': model, 'CHILD_TOKEN': 'secret', 'SILENT': '1' if silent else '0'}


@pytest.mark.asyncio
async def test_actual_child_gets_no_prompt_until_authenticated(tmp_path):
    from litetui.agent_supervisor import AgentProcess
    argv, env = child(tmp_path)
    process = AgentProcess()
    await process.start(argv, cwd=tmp_path, env=env)
    try:
        with pytest.raises(LaunchBlocked, match='ready'):
            await process.send_prompt('premature')
        await process.handshake(spec(tmp_path), child_id='child', conversation_id='convo',
                                token='secret', workspace=str(tmp_path), timeout=5)
        await process.send_prompt('quoted "text"\nsecond line')
        event = await process.receive(timeout=2)
        assert event['command'] == {'type':'prompt', 'text':'quoted "text"\nsecond line'}
    finally:
        assert await process.close(timeout=2)
    assert process.returncode is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('silent,model', [(True, 'fixture'), (False, 'fallback')])
async def test_bad_or_missing_handshake_closes_owned_child(tmp_path, silent, model):
    from litetui.agent_supervisor import AgentProcess
    argv, env = child(tmp_path, silent=silent, model=model)
    process = AgentProcess()
    await process.start(argv, cwd=tmp_path, env=env)
    with pytest.raises(LaunchBlocked):
        await process.handshake(spec(tmp_path), child_id='child', conversation_id='convo',
                                token='secret', workspace=str(tmp_path), timeout=.3 if silent else 5)
    assert process.returncode is not None
    assert not process.ready
