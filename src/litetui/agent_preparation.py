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
    """Schedule `coro` as a VISIBLE Textual worker (ON the loop — the THREAD
    offload is `coro`'s own job, via await_preparation), guaranteeing `cleanup`
    fires exactly once: on normal completion, on error, OR on a cancel BEFORE the
    coroutine's first real step (the case Worker.StateChanged never reports).

    A caller claims a lock synchronously and would wedge if a worker cancelled
    before its finally ran; `cleanup` is the release, made idempotent by the
    caller (it no-ops once the coro has settled).

    The work parks on a startup gate before doing anything, so it can NEVER run
    before the done-callback is armed — correct even under an eager task factory.
    Only after the callback is attached to worker._task is the gate opened.

    `started` lives in OUTER state, not in `_guarded`: a worker cancelled before
    its coroutine's first step never runs `_guarded`'s body/finally, so the
    terminal callback (or a fail-closed path) is what closes the never-awaited
    original coro. Attachment itself is guarded — a raising add_done_callback,
    an absent _task, or a scheduling failure all fail closed (cancel the worker,
    close the never-started original, release once). Returns the worker, or None.
    """
    import asyncio
    go = asyncio.Event()
    state = {"armed": False, "started": False, "fired": False}

    def _fire(*_a):
        if state["fired"]:
            return
        state["fired"] = True
        if not state["started"]:
            # The original coro was never awaited (cancel before first step,
            # gate never opened, or attach failed). Its owner (_guarded) may
            # never have run, so close it here.
            try:
                coro.close()
            except Exception:
                pass
        cleanup()

    async def _guarded():
        await go.wait()
        if not state["armed"]:
            return
        state["started"] = True
        await coro

    guarded = _guarded()
    try:
        worker = app.run_worker(guarded, group=group, exclusive=False, exit_on_error=False)
    except Exception as e:  # noqa: BLE001 — scheduling failure: neither coro was handed to a task
        guarded.close()
        _fire()                           # started False -> also closes the original coro
        if report is not None:
            report(e)
        return None
    # From here a task owns `guarded`; cancel the WORKER to unwind it (never
    # guarded.close(), which the task owns), and close only the never-entered
    # original in _fire.
    task = getattr(worker, "_task", None)
    if task is None:
        _cancel_quietly(worker)
        _fire()
        if report is not None:
            report(None)
        return None
    try:
        task.add_done_callback(_fire)     # fires on EVERY terminal state, incl. pre-first-step cancel
    except Exception as e:  # noqa: BLE001 — attach failure must fail closed, never wedge
        _cancel_quietly(worker)
        _fire()
        if report is not None:
            report(e)
        return None
    state["armed"] = True
    go.set()                              # arm THEN open the gate
    return worker


def _cancel_quietly(worker) -> None:
    try:
        worker.cancel()
    except Exception:
        pass


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