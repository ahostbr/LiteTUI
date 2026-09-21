"""Internal prepared-workspace orchestration; not a registered model tool yet."""
from litetui.agent_launcher import start_headless_child
from litetui.agent_supervisor import finish_child


async def run_prepared_child(spec, process, *, registry, inbox, parent, child_id,
                             workspace, data_root, branch, evidence,
                             supported_levels, notify, limit=1, timeout=300,
                             parent_conversation=None, prepare=None, before_start=None):
    """Claim before start, bind before prompt, persist/settle before notify.

    Runtime owns routing and persistent workspace/root preparation. A launch
    failure before verified conversation identity retains its claim for manual
    recovery rather than inventing an identity or silently releasing a slot.
    Cancellation during collection persists pending outcome via finish_child;
    its registry claim intentionally remains until explicit reconciliation.
    """
    from litetui.agent_ancestry import require_root_launcher
    require_root_launcher()
    registry.claim(parent, child_id, limit=limit, parent_conversation=parent_conversation)
    if prepare is not None:
        # Preparation executes only after the global claim, off the UI loop.
        # Cancellation joins the bounded writes; partial state stays retained.
        from litetui.agent_preparation import await_preparation
        prepared = await await_preparation(prepare)
        workspace, data_root, branch = prepared.workspace, prepared.data_root, prepared.branch

    bound = False

    def bind(ready):
        nonlocal bound
        registry.bind(parent, child_id, conversation_id=ready['conversation_id'],
                      pid=ready['pid'], created=ready['process_created'])
        bound = True

    if before_start is not None:
        before_start()
    import asyncio
    launch_outcome = None
    launch_cancelled = None
    try:
        await start_headless_child(spec, process, workspace=workspace, data_root=data_root,
                                   supported_levels=supported_levels, on_ready=bind)
    except (Exception, asyncio.CancelledError) as exc:
        if not bound:
            raise  # No verified identity: retain unknown claim, never invent a result.
        launch_cancelled = exc if isinstance(exc, asyncio.CancelledError) else None
        launch_outcome = {
            'status': 'cancelled' if launch_cancelled is not None else 'failed',
            'summary': f'Child prompt delivery failed: {type(exc).__name__}',
        }
    try:
        completion = await finish_child(process, inbox, parent=parent, child_id=child_id,
            branch=branch, evidence=evidence, notify=lambda event: None,
            timeout=timeout, data_root=data_root, launch_outcome=launch_outcome)
    except asyncio.CancelledError:
        # finish_child has already joined cleanup and persisted cancellation.
        # Settle only its matching confirmed outcome; unknown cleanup retains
        # the slot. Preserve cancellation even if reconciliation storage fails.
        try:
            event = inbox.for_child(parent, child_id)
            if event is not None and event['result'].get('cleanup', {}).get('state') == 'confirmed':
                registry.settle_completion(parent, child_id, inbox=inbox,
                                           completion_id=event['completion_id'])
        except Exception:
            pass  # durable claim/outcome remain available for startup recovery
        raise
    except Exception:
        if launch_cancelled is not None:
            raise launch_cancelled
        raise
    try:
        result = inbox.get(parent, completion)
        if result['cleanup']['state'] == 'confirmed':
            registry.settle_completion(parent, child_id, inbox=inbox, completion_id=completion)
    except Exception:
        if launch_cancelled is not None:
            raise launch_cancelled
        raise
    if launch_cancelled is not None:
        raise launch_cancelled  # Durable outcome will replay; preserve caller cancellation.
    notify({'completion_id': completion, 'result': result})
    return completion
