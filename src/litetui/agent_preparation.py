"""Prepare retained launch locations outside the parent checkout."""
from dataclasses import dataclass
from pathlib import Path
from litetui.agent_launcher import LaunchBlocked, validate_capabilities
from litetui.agent_workspace import create_worktree


@dataclass(frozen=True)
class PreparedChild:
    workspace: Path
    data_root: Path
    branch: str
    baseline: str


def prepare_child(spec, *, storage, child_id, baseline, supported_levels):
    # Admission before filesystem effects. Explicit-workspace mutation and local
    # engines remain unavailable until their ownership/admission gates exist.
    if spec.headed or spec.backend != 'codex' or spec.workspace_mode != 'worktree':
        raise LaunchBlocked('Only isolated headless hosted worktree launch is integrated')
    validate_capabilities(spec, supported_levels)
    import re
    if not isinstance(child_id, str) or not re.fullmatch(r'[0-9a-f]{32}', child_id):
        raise LaunchBlocked('Invalid child identity')
    root = Path(storage).resolve() / child_id
    if root.exists():
        raise LaunchBlocked('Child storage already exists; retained for recovery')
    # create_worktree independently rejects destinations nested in the actual
    # Git root, including when spec.workspace names a subdirectory.
    prepared = create_worktree(spec.workspace, root / 'workspace',
                               baseline=baseline, child_id=child_id)
    data = root / 'data'
    data.mkdir(exist_ok=False)
    return PreparedChild(prepared.path, data, prepared.branch, prepared.commit)
