"""Isolated coding workspaces; never stash, reset, copy or integrate a parent."""
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class WorkspaceBlocked(ValueError):
    pass


@dataclass(frozen=True)
class AgentWorkspace:
    path: Path
    branch: str
    commit: str
    repository: Path


def _git(root, *args):
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceBlocked(f'Git workspace operation failed: {exc}') from exc
    if result.returncode:
        raise WorkspaceBlocked(f'Git workspace operation failed: {result.stderr.strip()}')
    return result.stdout.strip()


def create_worktree(workspace, destination, *, baseline, child_id):
    """Create one retained branch at a resolved commit, without inheriting dirt.

    Failed Git operations are reported, not destructively cleaned up: an
    interrupted checkout can leave inspectable worktree/branch state. Retrying
    with the same destination is intentionally refused rather than overwriting.
    """
    if not isinstance(child_id, str) or not re.fullmatch(r'[0-9a-f]{32}', child_id):
        raise WorkspaceBlocked('Invalid child identity')
    if not isinstance(baseline, str) or not baseline or baseline.startswith('-'):
        raise WorkspaceBlocked('Explicit Git baseline required')
    root = Path(workspace).resolve()
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise WorkspaceBlocked('Child destination already exists')
    repository = Path(_git(root, 'rev-parse', '--show-toplevel')).resolve()
    # Do not create artifacts inside the parent's working tree.
    if target.resolve().is_relative_to(repository):
        raise WorkspaceBlocked('Child destination must be outside the parent Git workspace')
    commit = _git(repository, 'rev-parse', '--verify', '--end-of-options', baseline + '^{commit}')
    branch = 'litetui-agent/' + child_id
    from litetui.shared_state import coordinated_write
    # Git's own multi-step worktree setup is not a destination claim: racing
    # adds can both fail after creating partial metadata. Claim the stable
    # sibling lock before the final existence check and Git mutation.
    with coordinated_write(target):
        if target.exists() or target.is_symlink():
            raise WorkspaceBlocked('Child destination already exists')
        _git(repository, 'worktree', 'add', '-b', branch, str(target), commit)
    return AgentWorkspace(target, branch, commit, repository)
