# T0338 same-reviewer bounded R1/R2/R3 candidate

This is the one authorized fix round following REQUEST-CHANGES on 3025a62.
Only two production files change, implementing the three requested mechanisms.

- R1: under the existing conversation audit lock, unresolved intent IDs and
  retained revisions without intent block every fresh mutation across IDs/names,
  before retention/body/manifest/audit writes. Historical duplicate/ACK paths
  remain before the barrier and never reapply. Completed supersession remains
  available; uncertain evidence is preserved pending explicit inspection.
- R2: initial APPLY returns APPLIED_NOTIFICATION_UNKNOWN and notification_sent
  null. APPLIED is reserved for successful fingerprint-bound notification ACK.
  No automatic resend; timeout/cancel/FAILED/ACK_UNKNOWN remain honest.
- R3: old/custom generic request copies append host guidance to their FIRST
  string system row, preserve the existing prefix and stored transcript, and
  reject multiple/noninitial/nonstring system rows. If none exists, insert one
  initial system row. Native Claude and Codex production paths are unchanged.

Regressions: tests/test_prompt_review_t0338.py exercises cross-ID A/B injected
publication and pre-intent failures with complete evidence byte comparisons,
historical duplicate/ACK without replay, completed supersession/reset, leader
cancellation, actual _request_messages -> OpenAITransport -> validating captured
single-system provider boundary for override/reset, and malformed system refusal.
Existing assertions are updated only where the explicitly corrected R2/R3
contract changed; no broad expectation fitting.

Candidate is committed BEFORE test invocation. Candidate SHA/tree and pretest
production/test blob hashes are captured externally in the owned worktree
artifacts receipt and sent to leader, followed by original native test call,
start/completion task/log identifiers. No posttest source edits are planned.
Required validation: new regression file plus previous exact 14-file affected
selection (not full suite). Same Dijkstra rereview and merge gates remain pending.
