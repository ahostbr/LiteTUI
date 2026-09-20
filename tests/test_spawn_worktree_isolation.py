import subprocess
from pathlib import Path
import pytest


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def repo(tmp_path):
    root = tmp_path / 'parent with spaces'
    root.mkdir()
    git(root, 'init')
    git(root, 'config', 'user.name', 'Fixture')
    git(root, 'config', 'user.email', 'fixture@example.invalid')
    (root / 'source.txt').write_text('committed\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'baseline')
    return root


def test_child_uses_explicit_commit_not_dirty_parent(tmp_path):
    from litetui.agent_workspace import create_worktree
    root = repo(tmp_path)
    baseline = git(root, 'rev-parse', 'HEAD')
    (root / 'source.txt').write_text('parent dirty\n')
    (root / 'untracked.txt').write_text('private uncommitted\n')
    before = git(root, 'status', '--porcelain')
    child = create_worktree(root, tmp_path / 'child with spaces', baseline=baseline, child_id='a' * 32)
    assert child.commit == baseline
    assert (child.path / 'source.txt').read_text() == 'committed\n'
    assert not (child.path / 'untracked.txt').exists()
    (child.path / 'source.txt').write_text('child edit\n')
    assert (root / 'source.txt').read_text() == 'parent dirty\n'
    assert git(root, 'status', '--porcelain') == before
    assert git(child.path, 'branch', '--show-current') == child.branch


def test_non_git_and_existing_destination_fail_without_changes(tmp_path):
    from litetui.agent_workspace import create_worktree, WorkspaceBlocked
    with pytest.raises(WorkspaceBlocked, match='Git'):
        create_worktree(tmp_path, tmp_path / 'child', baseline='HEAD', child_id='b' * 32)
    root = repo(tmp_path)
    destination = tmp_path / 'existing'
    destination.mkdir()
    (destination / 'keep').write_text('untouched')
    with pytest.raises(WorkspaceBlocked, match='exists'):
        create_worktree(root, destination, baseline='HEAD', child_id='b' * 32)
    assert (destination / 'keep').read_text() == 'untouched'


@pytest.mark.parametrize('child_id', ['../escape', '--force', '', 'a b', 'x' * 32])
def test_invalid_child_identity_has_no_git_side_effects(tmp_path, child_id):
    from litetui.agent_workspace import create_worktree, WorkspaceBlocked
    root = repo(tmp_path)
    before = git(root, 'branch', '--list')
    with pytest.raises(WorkspaceBlocked, match='identity'):
        create_worktree(root, tmp_path / 'child', baseline='HEAD', child_id=child_id)
    assert git(root, 'branch', '--list') == before
    assert not (tmp_path / 'child').exists()


def test_concurrent_destination_claim_never_overwrites_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from litetui.agent_workspace import create_worktree, WorkspaceBlocked
    root = repo(tmp_path)
    barrier = Barrier(2)
    def create(identity):
        barrier.wait()
        try:
            return create_worktree(root, tmp_path / 'shared target', baseline='HEAD', child_id=identity)
        except WorkspaceBlocked:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(create, ['a' * 32, 'b' * 32]))
    assert sum(result is not None for result in results) == 1
    assert (tmp_path / 'shared target' / 'source.txt').read_text() == 'committed\n'
    assert git(root, 'status', '--porcelain') == ''


def test_invalid_baseline_and_nested_destination_leave_parent_untouched(tmp_path):
    from litetui.agent_workspace import create_worktree, WorkspaceBlocked
    root = repo(tmp_path)
    for destination, baseline in [(root / 'nested', 'HEAD'), (tmp_path / 'child', '--help'),
                                  (tmp_path / 'child', 'does-not-exist')]:
        with pytest.raises(WorkspaceBlocked):
            create_worktree(root, destination, baseline=baseline, child_id='a' * 32)
        assert not destination.exists()
    assert git(root, 'status', '--porcelain') == ''
