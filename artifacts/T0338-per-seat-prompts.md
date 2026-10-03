# Per-seat prompt changes (T0338)

## Commands

Use the installed LiteTUI command (or `python -m litetui.cli` with this source on PYTHONPATH):

```
litetui prompt show Worker ordinary-instructions --convo <conversation-uuid>
litetui prompt diff Worker ordinary-instructions --convo <conversation-uuid>
litetui prompt set Worker ordinary-instructions --convo <conversation-uuid> --via Leader --file <utf8-file> --reason "Improve verification habits"
litetui prompt set Worker seat-instructions --convo <conversation-uuid> --via Leader --file <utf8-file> --reason "Task-specific guidance"
litetui prompt reset Worker seat-instructions --convo <conversation-uuid> --via Leader --reason "Withdraw task guidance"
```

Only named agent-owned stores are supported; legacy `.convos` targets fail closed.
The UUID must identify an existing transcript in that agent's authoritative catalog.
A mutation requires both actual live named-agent owners. `--via` selects the
leader's existing private local RPC capability; it is NOT a claimed sender id.
The target must be that leader's current registered child and own precisely the
requested CURRENT materialized conversation. No inactive conversation is opened
or edited. No target agent/session lease is acquired by the CLI or leader.

`REQUESTED <uuid>` is not success. `APPLIED` means the target owner published and
recorded the mutation and notification transport reported success; it does not
prove the model consumed the notification. `APPLIED_NOTIFICATION_FAILED`,
`APPLIED_NOTIFICATION_UNKNOWN`, and `APPLIED_NOTIFICATION_ACK_UNKNOWN` are partial
outcomes. If a request times out, inspect the target audit using the printed UUID;
never blindly submit a new request. An explicit `--request-id` deduplicates a
confirmed mutation, not inbox delivery. A duplicate does not silently resend mail.
There is no exactly-once notification claim. Intent without applied evidence
blocks every fresh mutation in that conversation, across request IDs and mutable
names, pending inspection. Retained revisions without audit intent also block
fresh mutation under the same audit lock. Historical duplicate lookups and valid
ACKs remain allowed without replay. The first owner APPLY explicitly returns
`APPLIED_NOTIFICATION_UNKNOWN` with `notification_sent: null`; only a successful
fingerprint-bound notification ACK produces `APPLIED`. Notification acknowledgements are bound to
the entire original request fingerprint and cannot mutate or replay it.

## Scope and immutable floor

Mutable files: `ordinary-instructions.md`, `seat-instructions.md`, `compact.md`,
`card-summary.md`, `wake-after-compact.md`. Ordinary instructions are exactly the
former systemprompt trailing Engineering practices section; the preceding
systemprompt bytes are unchanged. Existing host sections retain their original
order. With no override, the former default prompt text is recombined in its
original position, before plan/store/tool sections. Seat additions follow the
immutable composed host sections. They never grant authority over policy.

Global-only: `systemprompt.md`, `tool-denied.md`, `tools.md`, `claude-tools.md`,
`plan-mode.md`, `conversation-store.md`, `harness-capabilities.md`, all security
prompts and every unknown name. Any scoped copy, including inactive metadata,
is refused. Enforcement and global floor parsers are unchanged.

Each published copy records the global text/SHA it branched from. `diff` reports
both changes from current global text and drift from its branched global version.
Audit rows in `<conversation>/prompts/audit.jsonl` record actual leader identity,
time, reason, diff, request fingerprint, intent/applied and notification outcome.
Prior text and metadata survive in `prompts/revisions/`. Reset retains text but
sets metadata inactive; the next turn falls back to the current global file.
Managed manifests carry a normalized body SHA and publish before the body. An
interrupted set with missing/mismatched text fails closed; an interrupted audit
receipt remains UNKNOWN even if both files are consistent. Do not blindly retry
an intent-only request: inspect retained revision and audit, then recover locally.
A historical duplicate explicitly reports `current_revision: false` if a later
request superseded it; it never replays old text. Retained mutable temporary files
are diagnostic only. File flush + atomic rename are used on all platforms; POSIX
also fsyncs directories. Windows directory fsync is unavailable here, so no
additional power-loss durability guarantee is claimed. Same-OS-user manual edits
to a managed body must also match its manifest; unmanaged allowlisted copies
remain readable.

## Turn/backend behavior and release limitation

A main turn has one mtime-checked snapshot. A change during that turn is visible
on the NEXT turn, not to an ongoing tool round. Side-call/compaction prompt reads
are refreshed independently without replacing an in-flight main snapshot.
Generic request copies replace only explicitly host-owned ordinary text, keeping
stored transcript, custom system messages, index and memory snapshots intact.
Legacy/custom generic transcripts without host provenance are never guessed or
stripped: explicit scoped configuration appends host-authored current guidance to
the FIRST system message in the outgoing request copy only, including reset. Its
custom prefix and stored transcript remain unchanged. If no system exists, one
is inserted first. Multiple, noninitial or nonstring system rows explicitly refuse
scoped guidance rather than violating single-system model templates. Earlier
ordinary guidance remains history, so these seats cannot claim bytewise replacement.
Codex uses its existing instruction digest / additionalContext wire seam.

Fresh Claude native sessions contain the immutable floor ONLY, with current
ordinary guidance sent in a clearly host-authored per-turn context envelope.
The user's original input remains separate in the delivery ledger and transcript;
the sent ordinary context is recorded as host metadata. Resets send current
global ordinary text and an empty seat section to withdraw earlier additions.
Old native history remains history; prompt guidance is not an enforcement sandbox.

**A pre-split Claude session cannot accept an ordinary/seat override:** the leader
gets `This Claude seat needs one /claude new to accept per-agent prompts` before
mutation. The user must explicitly choose one new segment; there is no automatic
rotation or process restart. After that, hot reload needs no further new session.
Manually placed ordinary overrides in a legacy session also hold the turn unsent.
Compatibility is an explicit `host_policy_version: 1` ledger field written only
when the host first constructs an unbound floor-only segment. Missing, malformed
or quoted marker text never upgrades an old segment; old bytes/native ids remain
unchanged on load/save/resume. Generic request-copy fallback is not the native
Claude query path and does not override this refusal.

Incomplete/mismatched managed pairs hold actual admission unsent with a prompt
configuration error, preserving stored immutable floor. Snapshot reads verify
pre/post signatures and the loaded body against manifest digest; a snapshot that
spans publication is refused, not silently mixed. Torn/malformed audit tails and
orphan retained revisions are preserved and explicitly block further mutation
pending inspection. A post-commit local UI notice failure is reported separately
without disguising the known applied outcome.

## Trust boundary

Existing private local RPC authentication is retained (loopback, token, nonce,
owner identity). Only two local-only operations are added; neither is exposed
through public GUI/stdout RPC. Leader request uses the actual app's owned seat.
The target receives an actor assertion from that trusted route; it is NOT
cryptographically authenticated against another same-OS-user process holding the
private capability. Existing inbox and registry data have the same OS-user trust
boundary. Linked/reparse paths, traversal, foreign seat/conversation, unknown
schema fields and oversized/non-UTF8 prompt bytes are rejected. These are scoped
location/ownership checks, not protection against concurrent malicious filesystem
replacement by the same OS user. No bulk rollout, live-seat edits or restart is
performed by this patch's tests.
