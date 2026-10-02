"""Relocated literal Git reader waiver never lifts schedule writer protection."""
import pytest

from litetui import deny_floor
from litetui import tool_policy as tp


@pytest.mark.parametrize('tool_name', ['bash', 'powershell'])
@pytest.mark.parametrize('relocation', ['-C {root}', '--git-dir={root}/.git',
                                       '--git-dir {root}/.git --work-tree={root}'])
@pytest.mark.parametrize('verb, refused', [
    ('worktree list', False), ('status', False), ('log -- jobs.json', False),
    ('show HEAD:jobs.json', False), ('diff -- jobs.json', False),
    ('rev-parse --show-toplevel', False), ('branch --list', False),
    ('commit -m msg', True), ('checkout -- jobs.json', True),
    ('worktree add other', True), ('worktree remove other', True),
    ('config user.name someone', True), ('push', True),
    ('branch --list -D old', True), ('worktree list > {root}/jobs.json', True),
    ('diff --ext-diff', True), ('diff --textconv', True),
    ('show --ext-diff HEAD', True), ('log --textconv -p', True),
    ('-c diff.external=program diff', True), ('--config=diff.external=program diff', True),
    ('--exec-path=program diff', True), ('--paginate diff', True), ('difftool', True),
    ('diff --no-ext-diff --no-textconv', False),
])
def test_relocated_git_reader_and_writer_policy(tmp_path, relocation, verb, refused, tool_name):
    protected = tmp_path / 'protected'
    (protected / 'src' / 'litetui').mkdir(parents=True)
    (protected / 'jobs.json').write_text('[]', encoding='utf-8')
    plain = tmp_path / 'plain'
    plain.mkdir()
    command = 'git ' + relocation.format(root=protected.as_posix()) + ' ' + verb.format(root=protected.as_posix())
    # LiteTUI delegates schedule-seat checks separately; canonical jobs=True
    # protects hooked agents while normal tool policy still consumes this module.
    floor = deny_floor.refusal(command, plain)
    assert bool(floor) is refused
    decision = tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, {'command': command}, plain,
                           tool_name=tool_name)
    if not refused:
        assert decision.action == tp.ALLOW
