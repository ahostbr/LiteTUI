"""Prepare retained launch locations outside the parent checkout."""
from dataclasses import dataclass
from pathlib import Path
from litetui.agent_admission import admit_launch
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
    # Admission before filesystem effects: a local engine needs a verified
    # capacity reservation (the WS3 resolver's verdict) and headed /
    # explicit-workspace launches are not integrated. The composed service
    # holds no reservation, so every local request is refused here -- before
    # any filesystem effect -- with a distinct reason per case.
    verdict = admit_launch(spec)
    if not verdict.admitted:
        raise LaunchBlocked(verdict.reason)
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
        # Textual's Task runs Worker._run, which may be cancelled before it
        # ever awaits our wrapper. Only its terminal callback may close that
        # wrapper; failure paths must not close a coroutine owned by a live task.
        if _a and isinstance(_a[0], asyncio.Task) and _a[0].done():
            guarded.close()
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
    try:
        task = getattr(worker, "_task", None)
    except Exception as e:  # noqa: BLE001 — a raising descriptor must not escape uncleaned
        _cancel_quietly(worker)
        _fire()
        if report is not None:
            report(e)
        return None
    # Require a REAL asyncio.Task, not merely non-None. An arbitrary object whose
    # add_done_callback fired _fire synchronously would release the claim and
    # then we'd open the gate and run the op UNCLAIMED. Anything else fails closed.
    if not isinstance(task, asyncio.Task):
        _cancel_quietly(worker)
        # No live asyncio.Task was exposed, so nothing runs our wrapper (a real Textual
        # worker always does); the gate is still closed, so `guarded` is at most
        # CORO_CREATED — close it here rather than leak a never-awaited coroutine.
        import inspect
        if inspect.getcoroutinestate(guarded) == inspect.CORO_CREATED:
            guarded.close()
        _fire()
        if report is not None:
            report(None)
        return None
    try:
        _attach(task, _fire)              # fires on EVERY terminal state, incl. pre-first-step cancel
    except Exception as e:  # noqa: BLE001 — attach failure must fail closed, never wedge
        _cancel_quietly(worker)
        # Textual owns Worker._run, not our as-yet unentered wrapper. With
        # the gate closed, closing that CREATED nested coroutine is safe even
        # if the cancelled Worker later attempts to await it (no work can run).
        # Never close a coroutine directly owned by the live asyncio Task.
        import inspect
        if (task.get_coro() is not guarded
                and inspect.getcoroutinestate(guarded) == inspect.CORO_CREATED):
            guarded.close()
        _fire()
        if report is not None:
            report(e)
        return None
    if task.done() or state["fired"]:
        # Raced to a terminal state (or the callback already fired): do NOT open
        # the gate — running the op now would run it against a released claim.
        _cancel_quietly(worker)
        if not state["fired"]:
            _fire()
        return None
    state["armed"] = True
    go.set()                              # arm THEN open the gate
    return worker


def _attach(task, cb) -> None:
    """The done-callback attach seam (a test can monkeypatch this to force an
    attach failure without weakening run_guarded's real-Task type check)."""
    task.add_done_callback(cb)


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