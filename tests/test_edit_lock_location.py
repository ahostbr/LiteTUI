"""User-file edit locks must not leave worktree debris, even after a crash."""
import multiprocessing
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from litetui import file_state, file_tools
from litetui.shared_state import OwnershipError, coordinated_write


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    return home


def test_edit_leaves_no_lock_in_worktree(tmp_path, home, monkeypatch):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    # A seat's configured data root may itself be the worktree.
    monkeypatch.setenv('LITETUI_DATA_ROOT', str(worktree))
    target = worktree / 'target.txt'
    target.write_text('old', encoding='utf-8')
    file_state.record_read(target)
    result = file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'})
    assert result.startswith('Edited'), result
    assert target.read_text(encoding='utf-8') == 'new'
    assert list(worktree.iterdir()) == [target]


def test_refused_edit_leaves_no_lock_in_worktree(tmp_path, home):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    target = worktree / 'missing.txt'
    result = file_tools.tool_edit({'path': str(target), 'old_string': 'old', 'new_string': 'new'})
    assert result.startswith('[error] file not found'), result
    assert list(worktree.iterdir()) == []


def _hold_edit_lock(target, home, connection):
    # Spawned child uses a different seat root, but the same user's lock space.
    os.environ['LITETUI_DATA_ROOT'] = str(home / 'other-seat')
    with (patch.object(Path, 'home', return_value=home),
          coordinated_write(target, user_file=True)):
        connection.send('acquired')
        connection.recv()
    connection.close()


@pytest.mark.parametrize('crash', [False, True])
def test_same_target_excluded_until_release_or_crash(tmp_path, home, monkeypatch, crash):
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    target = worktree / 'target.txt'
    target.write_text('old', encoding='utf-8')
    monkeypatch.setenv('LITETUI_DATA_ROOT', str(worktree))
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_hold_edit_lock, args=(target, home, child))
    process.start()
    child.close()
    try:
        assert parent.poll(10), 'child did not acquire the edit lock'
        assert parent.recv() == 'acquired'
        # Relative spelling and Windows case aliases must select the same lock.
        monkeypatch.chdir(worktree)
        alias = Path('TARGET.TXT' if os.name == 'nt' else './target.txt')
        with (pytest.raises(OwnershipError),
              coordinated_write(alias, timeout=0, user_file=True)):
            pytest.fail('two processes acquired the same target')
        # Distinct targets are not serialized behind a global lock.
        with coordinated_write(worktree / 'other.txt', timeout=0, user_file=True):
            pass
        assert list(worktree.iterdir()) == [target]
        if crash:
            process.terminate()
        else:
            parent.send('release')
        process.join(10)
        assert not process.is_alive()
        if not crash:
            assert process.exitcode == 0
        with coordinated_write(target, timeout=0, user_file=True):
            pass
        assert list(worktree.iterdir()) == [target]
    finally:
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
