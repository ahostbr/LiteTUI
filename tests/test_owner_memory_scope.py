"""Owner mode: the memory folder's NOW.md and DOMAIN.md are SELF_STORE, nothing else is."""
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import tool_policy as policy

PROFILES = (policy.STRICT, policy.INTERACTIVE, policy.AUTONOMOUS)


@pytest.fixture
def owner_dir(tmp_path):
    directory = tmp_path / 'widgets-memory'
    directory.mkdir()
    for name in ('SOUL.md', 'NOW.md', 'DOMAIN.md'):
        (directory / name).write_text('x', encoding='utf-8')
    return directory


def _decide(profile, target, workspace, owner, **kwargs):
    return policy.evaluate(profile, policy.WRITE_POLICY, {'path': str(target)}, workspace,
                           active_owner_memory_dir=owner, **kwargs)


@pytest.mark.parametrize('profile', PROFILES)
@pytest.mark.parametrize('name', ['NOW.md', 'DOMAIN.md'])
def test_the_two_working_files_are_self_store_and_never_asked(tmp_path, owner_dir, profile, name):
    work = tmp_path / 'work'
    work.mkdir()
    decision = _decide(profile, owner_dir / name, work, owner_dir)
    assert policy.SELF_STORE in decision.capabilities
    assert decision.action == policy.ALLOW


def test_the_charter_is_not_self_store(tmp_path, owner_dir):
    decision = _decide(policy.STRICT, owner_dir / 'SOUL.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


@pytest.mark.parametrize('relative', [
    '../NOW.md', 'sub/NOW.md', 'NOW.md.bak', 'NOTES.md', 'now.md.txt', 'memories/NOW.md',
    '.owner.lock', 'settings.json'])
def test_siblings_escapes_and_other_files_are_not_self_store(tmp_path, owner_dir, relative):
    (tmp_path / 'NOW.md').write_text('outside')
    decision = _decide(policy.STRICT, owner_dir / relative, tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_another_dir_with_the_same_file_names_is_not_self_store(tmp_path, owner_dir):
    other = tmp_path / 'other'
    other.mkdir()
    decision = _decide(policy.STRICT, other / 'NOW.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_tool_args_cannot_choose_the_owner_dir(tmp_path, owner_dir):
    other = tmp_path / 'other'
    other.mkdir()
    args = {'path': str(other / 'NOW.md'), 'active_owner_memory_dir': str(other)}
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY, args, tmp_path / 'work',
                               active_owner_memory_dir=owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
                               {'path': str(owner_dir / 'NOW.md'),
                                'active_owner_memory_dir': str(owner_dir)}, tmp_path / 'work')
    assert policy.SELF_STORE not in decision.capabilities


def test_without_an_owner_dir_nothing_changes(tmp_path, owner_dir):
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
                               {'path': str(owner_dir / 'NOW.md')}, tmp_path / 'work')
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_a_linked_owner_dir_never_gets_the_exception(tmp_path, owner_dir):
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(owner_dir, target_is_directory=True)
    except OSError:
        pytest.skip('host cannot create symlinks')
    decision = _decide(policy.STRICT, alias / 'NOW.md', tmp_path / 'work', alias)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_a_linked_owner_dir_is_refused_even_when_the_target_uses_the_real_path(tmp_path, owner_dir):
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(owner_dir, target_is_directory=True)
    except OSError:
        pytest.skip('host cannot create symlinks')
    decision = _decide(policy.STRICT, owner_dir / 'NOW.md', tmp_path / 'work', alias)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_a_path_spelled_through_a_link_is_refused_even_for_the_real_dir(tmp_path, owner_dir):
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(owner_dir, target_is_directory=True)
    except OSError:
        pytest.skip('host cannot create symlinks')
    decision = _decide(policy.STRICT, alias / 'NOW.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_a_linked_working_file_never_gets_the_exception(tmp_path, owner_dir):
    outside = tmp_path / 'outside.md'
    outside.write_text('unchanged')
    (owner_dir / 'NOW.md').unlink()
    try:
        (owner_dir / 'NOW.md').symlink_to(outside)
    except OSError:
        pytest.skip('host cannot create symlinks')
    decision = _decide(policy.STRICT, owner_dir / 'NOW.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM
    assert outside.read_text() == 'unchanged'


def test_a_hardlinked_working_file_never_gets_the_exception(tmp_path, owner_dir):
    elsewhere = tmp_path / 'elsewhere.md'
    elsewhere.write_text('unchanged')
    (owner_dir / 'DOMAIN.md').unlink()
    os.link(elsewhere, owner_dir / 'DOMAIN.md')
    decision = _decide(policy.STRICT, owner_dir / 'DOMAIN.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_a_reparse_point_in_the_path_fails_closed(tmp_path, owner_dir, monkeypatch):
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == owner_dir:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', reparse)
    decision = _decide(policy.STRICT, owner_dir / 'NOW.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE not in decision.capabilities
    assert decision.action == policy.CONFIRM


def test_the_agent_home_files_stay_self_store_beside_an_owner_dir(tmp_path, owner_dir):
    home = tmp_path / 'home'
    home.mkdir()
    decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
                               {'path': str(home / 'memory.md')}, tmp_path / 'work',
                               active_agent_memory_root=home, active_owner_memory_dir=owner_dir)
    assert policy.SELF_STORE in decision.capabilities


def test_host_authorization_dir_comes_from_the_app_not_args():
    from litetui.app import LiteTUI
    source = inspect.getsource(LiteTUI._authorize_action)
    assert 'active_owner_memory_dir=getattr(self, \'_owner_memory_dir\', None)' in source
    assert "args.get('active_owner_memory_dir'" not in source


# -- review round: what SOUL.md really does per profile, pinned -----------------------------

@pytest.mark.parametrize("tool", ["write", "edit", "Write", "Edit"])
@pytest.mark.parametrize("profile,action", [
    (policy.STRICT, policy.CONFIRM), (policy.INTERACTIVE, policy.ALLOW), (policy.AUTONOMOUS, policy.ALLOW)])
def test_soul_md_is_judged_like_any_write_outside_the_workspace(tmp_path, owner_dir, tool, profile, action):
    decision = policy.evaluate(profile, policy.WRITE_POLICY, {'path': str(owner_dir / 'SOUL.md')},
                               tmp_path / 'work', tool_name=tool, active_owner_memory_dir=owner_dir)
    assert decision.action == action
    assert policy.SELF_STORE not in decision.capabilities and policy.EXTERNAL_WRITE in decision.capabilities


@pytest.mark.skipif(os.name != 'nt', reason='NTFS resolves case variants and trailing dots to one file')
@pytest.mark.parametrize('spelling', ['now.md', 'Now.Md', 'DOMAIN.MD', 'NOW.md.', 'NOW.md '])
def test_on_windows_case_variants_and_trailing_dots_name_the_same_file_so_they_match(tmp_path, owner_dir, spelling):
    # Matching follows the filesystem: these all open the same file, so they are not a bypass.
    decision = _decide(policy.STRICT, owner_dir / spelling, tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE in decision.capabilities


@pytest.mark.skipif(os.name != 'nt', reason='case-insensitive naming is a Windows filesystem property')
def test_on_windows_a_case_variant_of_an_absent_note_still_matches(tmp_path, owner_dir):
    # The note is not on disk yet (a first write), so resolve() cannot restore its case:
    # the comparison itself must follow the filesystem.
    (owner_dir / 'NOW.md').unlink()
    decision = _decide(policy.STRICT, owner_dir / 'now.md', tmp_path / 'work', owner_dir)
    assert policy.SELF_STORE in decision.capabilities
