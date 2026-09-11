# ADR-0004 — Fleet seat identity is process-stable

- **Status:** accepted implementation; documents T507-T5, verified for T619.
- **Supersedes:** [ADR-0003](0003-seat-identity-is-derived-from-the-conversation.md).
- **Code:** `harness.process_agent_id()`, `app.LiteTUI.__init__`, `app.LiteTUI._sync_seat_identity`.

## Current decision

The running LiteTUI process owns one fleet seat ID, derived by UUID5 from hostname and PID. New conversations and resumes do not rebind that seat or register another mailbox. The conversation UUID remains the transcript/store identity and is shown shortened in the footer; it need not equal the fleet agent ID used for messaging.

The former conversation-derived rebind path left stale registrations when changing conversations. Process-stable identity avoids that transition. `_sync_seat_identity` is now deliberately a no-op; the old `agent_id_for_convo()` helper is not the active constructor path.

## Consequences

- Changing conversations within one process preserves its fleet address.
- A restarted process generally has another PID and therefore another fleet ID. Discover the active agent before dispatching; neither an old ID nor a display name proves current availability.
- Multiple windows have distinct process seats even when they open the same conversation. This does not grant concurrent transcript writes any new safety guarantee.
- Heartbeats remain required for presence. Name, process seat and conversation identity are separate concerns.

ADR-0003 remains as historical evidence of why the earlier design was chosen, not an instruction to restore it. This note changes no runtime behavior.
