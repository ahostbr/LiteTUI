"""Wake already-committed parent history without appending another user turn."""
from uuid import uuid4
from litetui import hook_host, tool_policy


async def wake_parent(app, *, parent, receipts):
    if (app._chat_running() or getattr(app, '_gui_quitting', False)
            or getattr(app, '_pending_input', [])
            or getattr(app, '_stop_requested', False)
            or hasattr(app.backend, 'app_server')):
        return []
    conversation = app.convo_id
    store = app.store
    if (store.convo_id != conversation or not store.owned or store.pending or store.loading
            or store.convo_path is None
            or app.conversation != store.read(store.convo_path)[1]):
        return []
    # Do not bypass prompt_before policy. Hook-aware receipt admission must be
    # integrated separately; until then leave wake pending rather than evade it.
    hooks = hook_host.snapshot(app)
    if hooks.hooks or hooks.error:
        return []
    ids = receipts.claim_wake(parent, conversation)
    if not ids:
        return []
    app._active_tool_profile = tool_policy.unattended(app.settings.tool_policy_profile)
    app._hooks_suppressed = False
    app._hook_corrections = 0
    app._hook_source = 'child-result'
    app._hook_turn_id = str(uuid4())
    # Claim persisted before scheduling. Exceptions/cancellation leave it
    # uncertain, never auto-repeat tool side effects after a crash.
    worker = app._stream()
    await worker.wait()
    receipts.finish_wake(parent, conversation, ids)
    return ids
