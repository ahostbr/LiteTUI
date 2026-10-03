# T0338 review evidence

Owned branch: `work/T0338-AgentPromptsGammaSol`
Baseline: `8549039be164b36fb0553feb98c1be481fe46da1`
No shared repository changes, downloads, installs, restarts, live-app mutations,
public pushes, deletions or bulk rollout.

## Pain Diagnosis
Import-once mutable prompts and fixed mixed native system prefixes prevented an
individual seat from receiving next-turn improvements without changing shared
safety text. Unscoped writes would bypass current owner/leader authority.

## Composition Analysis
Use the existing private local capability and actual app/AgentSession/store
ownership; no external lease acquisition or new control inbox. Two private RPC
operations compose with ordinary Seat.send notification after publication.

## Refactoring
Narrow allowlist resolver and per-turn snapshots, owner-side publication with
retained revisions/audit, default floor/ordinary split preserving original order.
Fresh native Claude fixes only floor and records explicit host policy provenance;
current ordinary guidance rides real query separately from original user ledger.
Legacy ordinary/seat mutations refuse before writes; no automatic rotation.

## Evolution Assessment
Historical requests dedup without reapply/resend; current_revision discloses
supersession. Missing/mismatched managed pairs hold admission unsent. Manifest/body
and snapshot-spanning-publication tests exercise actual failure boundaries.
Torn audits/orphan retained snapshots are preserved and require local inspection;
there is deliberately no recovery/retry framework. POSIX directory fsync is used;
Windows claims file flush/atomic rename, not extra power-loss durability.
OS-user capability trust is not cryptographic actor auth or a hostile-user sandbox.
Old generic/custom transcripts preserve their system bytes and get separate host
request-copy guidance only for explicit scoped configuration/tombstones; native
legacy Claude never takes this fallback. Earlier native history remains history.

## Validation
Exact final affected selection:

```
python -m pytest tests/test_prompt_edges_t0338.py tests/test_prompt_overrides_t0338.py tests/test_prompt_commands_t0338.py tests/test_prompt_native_t0338.py tests/test_prompt_compose.py tests/test_paths.py tests/test_claude_compact.py tests/test_wake_after_compact.py tests/test_local_rpc_t0016.py tests/test_hook_boundaries.py tests/test_codex_app_server.py tests/test_claude_turn.py tests/test_claude_backend.py tests/test_claude_persistence.py -q
```

Final result: **303 passed in 49.35s**, recorded in task log
`C:/Projects/LiteTUI/output/tasks/t-1b381d716f644898917bb85fc2ae9bbf.log`.
Prior snapshot: 302 passed in 49.81s; added native quoted-marker legacy test then
native subset 4 passed in 3.27s. Never ran full suite.

`python temp-working-dir/t0338_verify.py` verified canonical immutable floor and
split recombination SHA256
`49cf484bc72956e5bd9ac96f911be4d551251e98258050f49e0525e551d2369b`;
10,512 untouched app.py lines match HEAD bytewise. Initial mixed-ending
normalization was reversed for untouched spans; app.py diff remains bounded.
Source split preserves the final blank line by design. Ruff/mypy unavailable;
no install attempted. Editing tool lock files remain untracked, not deliverables.

Tool-less child self-review log:
`C:/Projects/LiteTUI/output/tasks/t-cdc232ecef0440e0abe1b4faad72363c.log`.
Addressed real managed-pair, historical dedup, nonce traversal, escaped metadata,
retained-temp, audit-tail, orphan-retention, notice-failure, empty ordinary boundary,
seat-boundary and explicit Claude provenance defects. Child concern about registry
presence successor race is pre-existing unchanged code: follow-up, not silently
fixed. Actual turn admission owns mtime refresh; active native input captures its
snapshot and conversation compatibility check, preserving mid-turn stability.
This self-review is NOT Dijkstra's separate merge verdict.

## Self-evaluation
| Pain-driven | Composition | Minimalism | Requirement-driven | Shippable |
|---|---|---|---|---|
| 5 | 5 | 4 | 5 | 4 |

Separate Dijkstra review, leader merge/intent gate and human look remain pending.
No parent task completion or release authorization is claimed.
