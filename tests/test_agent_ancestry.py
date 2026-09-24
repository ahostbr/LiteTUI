import pytest
from litetui.agent_launcher import LaunchBlocked


@pytest.mark.parametrize('value', ['1', '2', '-1', 'invalid', '', '0.0'])
def test_nested_or_invalid_runtime_depth_cannot_spawn(monkeypatch, value):
    from litetui.agent_ancestry import require_root_launcher
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', value)
    with pytest.raises(LaunchBlocked): require_root_launcher()


def test_normal_root_launch_is_allowed(monkeypatch):
    from litetui.agent_ancestry import require_root_launcher
    monkeypatch.delenv('LITETUI_AGENT_DEPTH', raising=False)
    assert require_root_launcher() == 0
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', '0')
    assert require_root_launcher() == 0


@pytest.mark.asyncio
async def test_app_runtime_refuses_before_accessing_app_or_starting_process(monkeypatch):
    from litetui.agent_app_runtime import run_for_app
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', '1')
    with pytest.raises(LaunchBlocked, match='depth'):
        await run_for_app(None, None, None, registry=None, inbox=None, receipts=None,
            parent='parent', child_id='child', workspace=None, data_root=None,
            branch=None, evidence=[], supported_levels=[])


def test_preparation_refuses_before_filesystem_effects(monkeypatch, tmp_path):
    from litetui.agent_preparation import prepare_child
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', '1')
    with pytest.raises(LaunchBlocked, match='depth'):
        prepare_child(None, storage=tmp_path / 'children', child_id='a'*32,
                      baseline='HEAD', supported_levels=[])
    assert not (tmp_path / 'children').exists()

@pytest.mark.asyncio
async def test_lower_level_entrypoints_also_refuse_nested_calls(monkeypatch):
    from litetui.agent_runtime import run_prepared_child
    from litetui.agent_launcher import start_headless_child
    monkeypatch.setenv('LITETUI_AGENT_DEPTH', '1')
    with pytest.raises(LaunchBlocked, match='depth'):
        await start_headless_child(None, None, workspace=None, data_root=None, supported_levels=[])
    with pytest.raises(LaunchBlocked, match='depth'):
        await run_prepared_child(None, None, registry=None, inbox=None,
            parent='parent', child_id='child', workspace=None, data_root=None,
            branch=None, evidence=[], supported_levels=[], notify=None)

def test_real_interpreter_inherits_managed_depth_and_refuses(tmp_path):
    import os
    import subprocess
    import sys
    script = (
        'from litetui.agent_ancestry import require_root_launcher\n'
        'from litetui.agent_launcher import LaunchBlocked\n'
        'try:\n require_root_launcher()\n'
        'except LaunchBlocked:\n print("NESTED_BLOCKED")\n'
        'else:\n raise SystemExit("depth guard bypassed")\n')
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
        env={**os.environ, 'LITETUI_AGENT_DEPTH': '1'},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'NESTED_BLOCKED'