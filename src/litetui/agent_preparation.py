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


def run_guarded(app, coro, *, group, cleanup, report=None):
    """Run `coro` off-loop as a VISIBLE Textual worker, guaranteeing `cleanup`
    fires exactly once — on normal completion, on error, OR on a cancel BEFORE
    the coroutine's first real step (the case Worker.StateChanged never reports).

    A caller claims a lock synchronously and would deadlock/wedge if a worker
    cancelled before its finally ran; `cleanup` is the release, made idempotent
    by the caller (it no-ops once the coro has settled).

    The work parks on a startup gate before doing anything, so it can NEVER run
    before the done-callback is armed — correct even under an eager task factory
    (where create_task runs the coro synchronously to its first await). Only
    after the callback is attached to worker._task is the gate opened. If the
    private _task hook is ever absent, the gate is never opened, the parked
    worker is cancelled (no real work ran), the never-entered `coro` is closed,
    and cleanup runs once. Returns the worker, or None if it could not start.
    """
    import asyncio
    go = asyncio.Event()
    armed = {"ok": False}
    fired = {"done": False}

    def _fire(*_a):
        if not fired["done"]:
            fired["done"] = True
            cleanup()

    async def _guarded():
        started = False
        try:
            await go.wait()
            if not armed["ok"]:
                return
            started = True
            await coro
        finally:
            if not started:
                coro.close()          # the ORIGINAL coro was never awaited
    guarded = _guarded()
    try:
        worker = app.run_worker(guarded, group=group, exclusive=False, exit_on_error=False)
    except Exception as e:  # noqa: BLE001 — scheduling failure: nothing awaited either coro
        guarded.close()
        coro.close()
        _fire()
        if report is not None:
            report(e)
        return None
    task = getattr(worker, "_task", None)
    if task is not None:
        task.add_done_callback(_fire)     # fires on EVERY terminal state, incl. pre-first-step cancel
        armed["ok"] = True
        go.set()                          # arm THEN open the gate
        return worker
    # Private hook absent (unreachable while add_worker starts synchronously):
    # never arm, cancel the parked worker, release once. Fail closed.
    try:
        worker.cancel()
    except Exception:
        pass
    _fire()
    if report is not None:
        report(None)
    return None


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