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
    from litetui.agent_ancestry import require_root_launcher
    require_root_launcher()
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


async def await_preparation(prepare):
    """Keep bounded Git operations off the UI loop; join even on cancellation.

    A thread cannot be killed safely. The underlying Git calls have timeouts;
    cancellation waits for their terminal state rather than orphaning a write.
    Any partial storage and the registry claim remain retained for recovery.
    """
    import asyncio
    task = asyncio.create_task(asyncio.to_thread(prepare))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                raise
        except Exception:
            if cancelled:
                raise asyncio.CancelledError()
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result