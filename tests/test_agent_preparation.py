import subprocess
import pytest
from litetui.agent_launcher import validate_request, LaunchBlocked
from litetui.agent_preparation import prepare_child


def test_retained_preparation_uses_committed_baseline_and_separate_root(tmp_path):
    repo = tmp_path / 'parent'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init')
    git('config', 'user.name', 'fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (repo / 'answer.py').write_text('VALUE = 41\n')
    git('add', '.')
    git('commit', '-m', 'baseline')
    baseline = git('rev-parse', 'HEAD')
    (repo / 'answer.py').write_text('VALUE = 99\n')
    spec = validate_request({'prompt': 'task', 'backend': 'codex', 'model': 'model',
                             'workspace': str(repo)}, parent_profile='autonomous', depth=0)
    prepared = prepare_child(spec, storage=tmp_path / 'children', child_id='a'*32,
                             baseline=baseline, supported_levels=[])
    assert (prepared.workspace / 'answer.py').read_text() == 'VALUE = 41\n'
    assert (repo / 'answer.py').read_text() == 'VALUE = 99\n'
    assert prepared.data_root.is_dir()
    assert not prepared.data_root.is_relative_to(prepared.workspace)
    assert prepared.baseline == baseline
    with pytest.raises(LaunchBlocked, match='already exists'):
        prepare_child(spec, storage=tmp_path / 'children', child_id='a'*32,
                      baseline=baseline, supported_levels=[])


def test_unsupported_admission_has_no_filesystem_effects(tmp_path):
    spec = validate_request({'prompt': 'task', 'backend': 'ninfer', 'model': 'model',
                             'workspace': str(tmp_path)}, parent_profile='autonomous', depth=0)
    storage = tmp_path / 'children'
    with pytest.raises(LaunchBlocked):
        prepare_child(spec, storage=storage, child_id='a'*32, baseline='HEAD', supported_levels=[])
    assert not storage.exists()
