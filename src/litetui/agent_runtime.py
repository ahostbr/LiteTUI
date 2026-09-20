"""Internal prepared-workspace orchestration; not a registered model tool yet."""
from litetui.agent_launcher import start_headless_child
from litetui.agent_supervisor import finish_child


async def run_prepared_child(spec, process, *, registry, inbox, parent, child_id,
                             workspace, data_root, branch, evidence,
                             supported_levels, notify, limit=1, timeout=300):
    """Claim before start, bind before prompt, persist/settle before notify.

    Runtime owns routing and persistent workspace/root preparation. A launch
    failure before verified conversation identity retains its claim for manual
    recovery rather than inventing an identity or silently releasing a slot.
    Cancellation during collection persists pending outcome via finish_child;
    its registry claim intentionally remains until explicit reconciliation.
    """
    registry.claim(parent, child_id, limit=limit)

    def bind(ready):
        registry.bind(parent, child_id, conversation_id=ready['conversation_id'],
                      pid=ready['pid'], created=ready['process_created'])

    await start_headless_child(spec, process, workspace=workspace, data_root=data_root,
                               supported_levels=supported_levels, on_ready=bind)
    completion = await finish_child(process, inbox, parent=parent, child_id=child_id,
        branch=branch, evidence=evidence, notify=lambda event: None,
        timeout=timeout, data_root=data_root)
    result = inbox.get(parent, completion)
    if result['cleanup']['state'] == 'confirmed':
        registry.settle_completion(parent, child_id, inbox=inbox, completion_id=completion)
    notify({'completion_id': completion, 'result': result})
    return completion
