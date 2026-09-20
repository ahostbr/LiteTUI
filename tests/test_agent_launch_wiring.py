from pathlib import Path
import pytest
from litetui.agent_launcher import LaunchBlocked, validate_request


class Process:
    def __init__(self):
        self.calls = []
        self.closed = False
    async def start_python(self, **kwargs): self.calls.append(('start', kwargs))
    async def rpc_handshake(self, spec, **kwargs):
        self.calls.append(('handshake', kwargs))
        return {'conversation_id': 'real-child'}
    async def send_prompt(self, text): self.calls.append(('prompt', text))
    async def close(self): self.closed = True


def spec(tmp_path, **extra):
    return validate_request({'prompt': 'private task', 'backend': 'codex', 'model': 'requested',
        'workspace': str(tmp_path), 'reasoning_effort': 'low', **extra},
        parent_profile='interactive', depth=0)


@pytest.mark.asyncio
async def test_explicit_runtime_root_and_policy_before_prompt(tmp_path):
    from litetui.agent_launcher import start_headless_child
    process = Process()
    root = tmp_path / 'persistent-data'
    root.mkdir()
    await start_headless_child(spec(tmp_path), process, workspace=tmp_path,
                              data_root=root, supported_levels=['low'])
    assert [item[0] for item in process.calls] == ['start', 'handshake', 'prompt']
    kwargs = process.calls[0][1]
    assert kwargs['env']['LITETUI_DATA_ROOT'] == str(root.resolve())
    assert kwargs['args'] == ['--rpc', '--backend', 'codex', '--model', 'requested',
                              '--tool-profile', 'interactive', '--reasoning-effort', 'low']
    assert 'private task' not in repr(kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', [{'headed': True}, {'backend': 'ninfer'}])
async def test_unintegrated_paths_block_before_process(tmp_path, extra):
    from litetui.agent_launcher import start_headless_child
    process = Process()
    with pytest.raises(LaunchBlocked):
        await start_headless_child(spec(tmp_path, **extra), process, workspace=tmp_path,
                                  data_root=tmp_path, supported_levels=['low'])
    assert not process.calls


@pytest.mark.asyncio
async def test_bad_capabilities_block_before_start(tmp_path):
    from litetui.agent_launcher import start_headless_child
    process = Process()
    with pytest.raises(LaunchBlocked):
        await start_headless_child(spec(tmp_path), process, workspace=tmp_path,
                                  data_root=tmp_path, supported_levels=[])
    assert not process.calls


@pytest.mark.asyncio
async def test_prompt_transport_failure_closes_owned_child(tmp_path):
    from litetui.agent_launcher import start_headless_child
    process = Process()
    async def failed(text): raise OSError('pipe closed')
    process.send_prompt = failed
    with pytest.raises(OSError, match='pipe closed'):
        await start_headless_child(spec(tmp_path), process, workspace=tmp_path,
                                  data_root=tmp_path, supported_levels=['low'])
    assert process.closed

@pytest.mark.asyncio
@pytest.mark.parametrize('reject', [True, False])
async def test_identity_registration_must_commit_before_prompt(tmp_path, reject):
    from litetui.agent_launcher import start_headless_child
    process = Process()
    def on_ready(event):
        assert event['conversation_id'] == 'real-child'
        process.calls.append(('registered', event))
        if reject:
            raise OSError('registry unavailable')
    if reject:
        with pytest.raises(OSError, match='registry unavailable'):
            await start_headless_child(spec(tmp_path), process, workspace=tmp_path,
                data_root=tmp_path, supported_levels=['low'], on_ready=on_ready)
        assert process.closed
        assert not any(kind == 'prompt' for kind, _ in process.calls)
    else:
        await start_headless_child(spec(tmp_path), process, workspace=tmp_path,
            data_root=tmp_path, supported_levels=['low'], on_ready=on_ready)
        assert [kind for kind, _ in process.calls] == ['start', 'handshake', 'registered', 'prompt']