"""Wake already-committed parent history without appending another user turn."""
import json
from uuid import uuid4
from litetui import hook_host, seat_authority, restart_checkpoint
from litetui.turn_deferral import STREAM_DEFERRED


def _committed_wake(app, *, parent, receipts):
    """Prepare exact saved-history authority without claiming or appending."""
    from litetui import history_projection as projection
    from litetui import native_history
    from litetui.agent_inbox import _identity
    from litetui.agent_parent_delivery import receipt_message

    _identity(parent)
    events = receipts.pending_wakes(parent, app.convo_id)
    if not events:
        return None
    ids = [_identity(event['completion_id']) for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate pending child receipt identity')
    app._validate_owned_identity()
    app.store._validate_owned_transcript()
    owner = (app._agent_session, app.store, app.backend, app.conversation, app.convo_id, app.model_id)
    captured = native_history.capture_file(app.store.convo_path)
    if captured.raw is None or captured.meta.get('id') != app.convo_id or captured.messages != app.conversation:
        raise ValueError('Committed child history differs from its owned runtime')
    expected = {event['completion_id']: receipt_message(event) for event in events}
    seen = set()
    for line in captured.raw.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict) or record.get('completion_id') not in expected:
            continue
        ident = record['completion_id']
        if ident in seen or record.get('type') != 'msg' or record.get('message') != expected[ident]:
            raise ValueError('Committed child receipt record is duplicated or changed')
        seen.add(ident)
    if seen != set(ids):
        raise ValueError('Pending child wake lacks its exact committed history record')
    payload = {'delivery':'committed_history', 'source':'child-result',
               'tool_profile':seat_authority.turn_profile(app, 'child-result'),
               'text':f'Continuing from {len(ids)} saved child result(s).',
               'history':{'session_id':app.convo_id, 'transcript_sha256':captured.revision,
                          'context_sha256':projection.digest(captured.messages_bytes), 'receipt_ids':ids}}
    projection.encoded(payload, limit=65536)

    def validate():
        current = (app._agent_session, app.store, app.backend, app.conversation, app.convo_id, app.model_id)
        if any(before is not after for before, after in zip(owner[:4], current[:4])) or owner[4:] != current[4:]:
            raise ValueError('Committed child wake changed its owned runtime')
        app._validate_owned_identity()
        app.store._validate_owned_transcript()
        after = native_history.capture_file(app.store.convo_path)
        if after.raw != captured.raw or after.messages != app.conversation:
            raise ValueError('Committed child history changed before its wake claim')

    def claim():
        validate()
        if receipts.pending_wakes(parent, app.convo_id) != events:
            raise ValueError('Pending child receipts changed before wake claim')
        return receipts.claim_wake(parent, app.convo_id)

    return payload, validate, claim


async def wake_parent(app, *, parent, receipts):
    from litetui import background_input
    if background_input.activation_pending(app):
        # A pending handshake defers the canonical wake without claiming it.
        return []
    if (restart_checkpoint.is_held(app) or app._chat_running() or getattr(app, '_gui_quitting', False)
            or getattr(app, '_mcp_maintenance', False)
            or getattr(app, '_pending_input', [])
            or getattr(app, '_stop_requested', False)
            or hasattr(app.backend, 'app_server')):
        # Checked BEFORE claim_wake: a wake deferred during MCP maintenance must
        # not claim (and therefore finish) its receipts — it re-fires later.
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
    # T1043: checked BEFORE claim_wake, like the gates above: a seat below the
    # fleet floor wakes nothing, and the receipts stay pending for a fixed seat.
    why = seat_authority.floor_refusal(app, 'child-result')
    if why is not None:
        app._system(why)
        return []
    background_input.require_recapture_complete(app)
    value = background_input.bridge(app)
    if value is not None:
        if not background_input.enabled(app) or background_input.prompt_refusal(app):
            return []
        prepared = _committed_wake(app, parent=parent, receipts=receipts)
        if prepared is None:
            return []
        payload, validate, claim = prepared
        return await background_input.run_committed_wake(app, parent=parent, payload=payload,
            validate=validate, claim=claim,
            finish=lambda ids: receipts.finish_wake(parent, conversation, ids),
            release=lambda ids: receipts.release_wake(parent, conversation, ids))
    ids = receipts.claim_wake(parent, conversation)
    if not ids:
        return []
    # The chosen profile, whole; nobody is here, so a CONFIRM is refused
    # (`_hook_source` below is one of tool_policy.UNATTENDED_SOURCES).
    app._active_tool_profile = seat_authority.turn_profile(app, 'child-result')
    app._hooks_suppressed = False
    app._hook_corrections = 0
    app._hook_source = 'child-result'
    app._hook_turn_id = str(uuid4())
    # Claim persisted before scheduling. Exceptions/cancellation leave it
    # uncertain, never auto-repeat tool side effects after a crash.
    worker = app._stream()
    if (await worker.wait()) is STREAM_DEFERRED:
        # Deferred at the pre-generation gate: nothing streamed, so the claim is
        # provably side-effect free. Release it to pending for a clean re-fire,
        # and do NOT finish (the turn never ran). A real failure/cancellation
        # raises out of wait() instead and leaves the claim 'claimed'
        # (uncertain), never released.
        receipts.release_wake(parent, conversation, ids)
        return []
    receipts.finish_wake(parent, conversation, ids)
    return ids
