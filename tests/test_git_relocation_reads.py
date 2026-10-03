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


_KEM_CHAIN = (
    'git -C E:/SAS/ShadowsAndShurikens/.worktrees/KEMEditorGammaSol-T0321 status --short; '
    'git -C E:/SAS/ShadowsAndShurikens/.worktrees/KEMEditorGammaSol-T0321 branch --show-current'
)
_ANCESTRY_CHAIN = (
    "git -C C:/Projects/LiteSuite log --format='%H %P%n%s%n%b' "
    '3a4e8347b2dbd72b8a4d975b336350d987353957..3b60a4d999886ce48991449c6ff5728a130d418d; '
    'git -C C:/Projects/LiteSuite diff --stat '
    '3a4e8347b2dbd72b8a4d975b336350d987353957..3b60a4d999886ce48991449c6ff5728a130d418d; '
    'git -C C:/Projects/LiteSuite merge-base --is-ancestor '
    '3a4e8347b2dbd72b8a4d975b336350d987353957 3b60a4d999886ce48991449c6ff5728a130d418d'
)
_OID_A = '3a4e8347b2dbd72b8a4d975b336350d987353957'
_OID_B = '3b60a4d999886ce48991449c6ff5728a130d418d'


@pytest.mark.parametrize('shell', [None, 'powershell', 'bash'])
@pytest.mark.parametrize('command,workspace', [
    (_KEM_CHAIN, 'E:/SAS/ShadowsAndShurikens'),
    (_ANCESTRY_CHAIN, 'C:/Projects/.scratch/T0343/YouTubeThemeGammaSol/LiteSuite'),
])
def test_t0340_exact_relocated_read_chains_are_not_schedule_writes(command, workspace, shell):
    # Inert classifier inputs only: no Git subprocess or foreign workspace I/O.
    assert deny_floor.jobs_git_refusal(command, workspace, shell=shell) is None
    assert deny_floor.refusal(command, workspace, shell=shell) is None


@pytest.mark.parametrize('shell', [None, 'powershell', 'bash'])
@pytest.mark.parametrize('verb', [
    'branch --show-current',
    f'merge-base --is-ancestor {_OID_A} {_OID_B}',
    f'merge-base --is-ancestor {_OID_A.upper()} {_OID_B.upper()}',
])
def test_t0340_finite_new_git_reads_keep_protected_schedule_readable(tmp_path, shell, verb):
    command = f'git -C C:/Projects/LiteTUI {verb}'
    assert deny_floor.jobs_git_refusal(command, tmp_path, shell=shell) is None


@pytest.mark.parametrize('shell', [None, 'powershell', 'bash'])
@pytest.mark.parametrize('verb', [
    'branch --show-current -D old',
    'branch --show-current -m renamed',
    'branch --show-current new',
    'branch --list --show-current',
    'branch -D old',
    'branch new',
    f'merge-base {_OID_A} {_OID_B}',
    f'merge-base --all {_OID_A} {_OID_B}',
    f'merge-base --octopus {_OID_A} {_OID_B}',
    f'merge-base --fork-point {_OID_A} {_OID_B}',
    f'merge-base --is-ancestor {_OID_A}',
    f'merge-base --is-ancestor {_OID_A} {_OID_B} {_OID_A}',
    f'merge-base --is-ancestor {_OID_A[:-1]} {_OID_B}',
    f'merge-base --is-ancestor {_OID_A} {_OID_B}0',
    f'merge-base --is-ancestor {"g" * 40} {_OID_B}',
    f'merge-base --is-ancestor HEAD {_OID_B}',
    f'merge-base --is-ancestor $revision {_OID_B}',
    f'merge-base --is-ancestor {_OID_A} {_OID_B} --output=jobs.json',
    f'merge-base --is-ancestor {_OID_A} {_OID_B} --ext-diff',
    f'merge-base --is-ancestor {_OID_A} {_OID_B} --textconv',
    f'merge-base --is-ancestor {_OID_A} {_OID_B} --config=key=value',
    '-c alias.branch=program branch --show-current',
    '--config-env=key=ENV branch --show-current',
    '--exec-path=program branch --show-current',
    '--paginate branch --show-current',
    'branch --show-current --pager=program',
    'branch --show-current --output=jobs.json',
    'branch --show-current > jobs.json',
    f'merge-base --is-ancestor {_OID_A} {_OID_B} > jobs.json',
])
def test_t0340_new_git_read_shapes_do_not_waive_writes_or_unknowns(tmp_path, shell, verb):
    reason = deny_floor.jobs_git_refusal(f'git -C C:/Projects/LiteTUI {verb}', tmp_path, shell=shell)
    assert reason and 'jobs-file' in reason
