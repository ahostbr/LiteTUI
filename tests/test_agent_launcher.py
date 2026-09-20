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

@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows job containment')
async def test_owned_grandchild_dies_but_unrelated_process_survives(tmp_path):
    import subprocess
    from litetui.agent_supervisor import AgentProcess
    from litetui import jobkill
    script = tmp_path / 'tree.py'
    script.write_text('''import subprocess, sys, json
sys.stdin.readline()
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print(json.dumps({'pid':p.pid}), flush=True)
sys.stdin.read()
''')
    unrelated = subprocess.Popen([sys._base_executable, '-c', 'import time; time.sleep(60)'])
    process = AgentProcess()
    grandchild = None
    try:
        await process.start([sys._base_executable, str(script)], cwd=tmp_path)
        process.process.stdin.write(b'go\n')
        await process.process.stdin.drain()
        grandchild = (await process.receive(timeout=5))['pid']
        assert jobkill.alive(grandchild)
        assert await process.close(timeout=.2)
        for _ in range(40):
            if not jobkill.alive(grandchild):
                break
            await asyncio.sleep(.05)
        assert not jobkill.alive(grandchild)
        assert unrelated.poll() is None
    finally:
        await process.close(timeout=.2)
        unrelated.kill()
        unrelated.wait(timeout=5)
        if grandchild and jobkill.alive(grandchild):
            # Fixture cleanup only; the test recorded this exact spawned PID.
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(1, False, grandchild)
            if handle:
                kernel.TerminateProcess(handle, 1)
                kernel.CloseHandle(handle)

@pytest.mark.asyncio
async def test_bootstrap_waits_for_containment_and_preserves_import_environment(tmp_path):
    from litetui.agent_supervisor import AgentProcess, python_child_argv
    script = tmp_path / 'gated child.py'
    script.write_text("import json, os, sys, textual\nprint(json.dumps({'pid':os.getpid(),'args':sys.argv[1:],'textual':textual.__version__}),flush=True)\nsys.stdin.read()\n")
    argv = python_child_argv(script=script, args=['spaces and "quotes"'])
    process = AgentProcess()
    await process.start(argv, cwd=tmp_path, gated=True)
    try:
        event = await process.receive(timeout=5)
        assert event['pid'] == process.process.pid
        assert event['args'] == ['spaces and "quotes"']
        assert event['textual']
    finally:
        assert await process.close()

@pytest.mark.asyncio
async def test_bootstrap_does_not_execute_target_without_release(tmp_path):
    from litetui.agent_supervisor import AgentProcess, python_child_argv
    marker = tmp_path / 'executed'
    script = tmp_path / 'not yet.py'
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    process = AgentProcess()
    await process.start(python_child_argv(script=script), cwd=tmp_path)
    await asyncio.sleep(.1)
    assert not marker.exists()
    assert await process.close(timeout=.2)
    assert not marker.exists()

@pytest.mark.asyncio
async def test_cancelling_pending_handshake_reaps_owned_process(tmp_path):
    from litetui.agent_supervisor import AgentProcess, python_child_argv
    script = tmp_path / 'silent.py'
    script.write_text('import time\ntime.sleep(60)\n')
    process = AgentProcess()
    await process.start(python_child_argv(script=script), cwd=tmp_path, gated=True)
    task = asyncio.create_task(process.handshake(spec(tmp_path), child_id='child',
        conversation_id='convo', token='secret', workspace=str(tmp_path), timeout=30))
    await asyncio.sleep(.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert process.returncode is not None
    assert not process.ready
    assert await process.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows job containment')
async def test_containment_failure_never_releases_bootstrap(tmp_path, monkeypatch):
    from litetui.agent_supervisor import AgentProcess, python_child_argv
    from litetui import jobkill
    marker = tmp_path / 'must-not-exist'
    script = tmp_path / 'target.py'
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    monkeypatch.setattr(jobkill, 'assign', lambda job, pid: False)
    process = AgentProcess()
    with pytest.raises(LaunchBlocked, match='containment'):
        await process.start(python_child_argv(script=script), cwd=tmp_path, gated=True)
    assert process.returncode is not None
    assert not marker.exists()

@pytest.mark.asyncio
async def test_gated_installed_entrypoint_runs_without_checkout_cwd(tmp_path):
    from litetui.agent_supervisor import AgentProcess
    from litetui.version import __version__
    process = AgentProcess()
    await process.start_python(module='litetui.cli', args=['--version'], cwd=tmp_path)
    try:
        line = await asyncio.wait_for(process.process.stdout.readline(), 5)
        assert line.decode().strip() == f'litetui {__version__}'
        await asyncio.wait_for(process.process.wait(), 5)
        assert process.returncode == 0
    finally:
        await process.close()
