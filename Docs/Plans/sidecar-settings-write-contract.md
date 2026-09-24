# Native settings edit contract — design gate

**Status:** protocol direction approved by Sentinel; implementation and `/settings` replacement are separate gates. Parent LiteTUI is the only settings writer. The current Rust snapshot is read-only.

## Duplex protocol and ownership

- Retain per-child capability, version, 1 MiB maximum JSON-line frame, request ID, parent-bound stdin/stdout, bounded response timeout and process cleanup. Add child-originated `settings_patch` **event** with `{id, token, version, command:"settings_patch", payload:{changes:[{key,value,scope}], expected_revisions:{global,conversation}}}`. Its ID is in a child-owned namespace so it cannot be mistaken for a parent reply. Never allow arbitrary commands or paths; browser content cannot access the trusted shell or this channel.
- Parent reader validates shape/size/version/capability and the **exact operation allowlist** before dispatch; unknown operation or wrong token is dropped and logged without closing the pipe. Repeated abuse may be rate-limited, but not silently translated into a write. Parent marshals validated operations onto Textual's UI thread. Replies correlate by child request ID and contain per-destination persistence results, current revisions on conflict, per-field runtime statuses, and an explicit error if no write happened. Errors cannot be mistaken for saved-success.
- The app supplies the conversation identity; no child-provided conversation ID or storage path is accepted. Reject fields not in `SETTING_SPECS`, mismatched scope, invalid value type, excessive patch count or oversized frame. Do not echo secret values in logs/replies. Only one settings mutation in flight per child; duplicate IDs are rejected. The child has no direct filesystem settings write.

## Revisions, editable surface and actions

- The child drafts the fields exposed by the existing Textual settings UI only; use the matrix in `sidecar-settings-parity.md` and a field-by-field audit against `SETTING_SPECS` to decide editability. Environment/capability-locked fields, unknown fields, and sensitive values stay noneditable until specifically reviewed. Hooks/custom themes/MCP per-server toggles require separate handler/persistence audit, not generic field patches. Immediate actions (voice test/install, hotkey capture, model/backend actions, hook tests, reset, etc.) are **not** `settings_patch`; each needs its own narrowly authorized operation and verification before UI parity. No action operation is approved in this contract.
- Save calls `SettingsService.save_patch` with the draft's expected global/conversation revisions, then `settings_runtime.apply_saved_result` for successfully saved fields, on the authoritative parent path. Any stale destination revision returns an explicit conflict with reload/compare affordance; never retry/overwrite automatically. Mixed-destination partial success is shown distinctly (saved fields, conflicted fields, pending/failed runtime effects), and child refreshes its snapshot before further edits. Storage success and runtime application are separate.

## Cancellation, shutdown and failure

- Parent keeps UI responsive: async pipe reader/writer and bounded queue; no Textual event-loop blocking on child I/O. A cancel before dispatch discards the request; after dispatch begins it cannot undo a committed save, so wait for/obtain the authoritative result rather than claim cancellation. A timeout or lost reply is **indeterminate**, not an invitation to repeat a patch. Refresh snapshot/revisions to determine which destinations committed; do not silently retry.
- Unsaved draft lives only in the child *during normal operation*, but before planned shutdown parent asks for the draft and stores a recoverable copy under the owning conversation after explicit user resolution (save/discard/keep). On child crash/parent-pipe loss where the draft cannot be recovered, warn that unsaved edits were lost; do not claim recovery or a save. Planned shutdown that times out must preserve the last parent-received draft if available and label it potentially stale before terminating the child. Draft recovery format, sensitive-field handling, and exact user choices require tests before enabling writes.
- Child death mid-save: the parent-owned save may have partially or fully committed even if reply was lost. Finish or report the parent operation, record outcome/revisions and surface status in Textual; on reopen fetch a fresh snapshot. A child crash must not cancel a write mid-commit or cause a replay. Parent death terminates child via pipe EOF; no child retry or autonomous write.

## Gates

1. Test malformed/unknown/bad-token frames without losing the pipe; duplicate IDs, oversized frames, dropped replies, timeouts, cancellation and two simultaneous children.
2. Test stale revision, cross-destination partial result, environment lock, runtime pending/failure, secret redaction and child death mid-save.
3. Audit **every** Textual field/action and verify round trip against Textual. `/settings` remains Textual until this parity gate passes. No browser IPC is implied.
