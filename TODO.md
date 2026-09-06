# LiteTUI — TODO

One line per item, newest section at the top. The portfolio board (`C:/Projects/scripts/portfolio.py`)
reads the first open line of this file, so keep the next thing to do at the top.

## Compaction — measured on the 27B seat, 2026-09-06 (Ryan: "great info and easily fixed stuff")

Source: convo `.convos/83134335-5797-47e3-a6c7-1f0d3d51a4cf/convo.jsonl`, two compactions (00:11:40
and 00:42:05), model `ektome-qwen3.8-27b`, ~80 tok/s. Code: `src/litetui/app.py` (`_autocompact_due`,
`_maybe_autocompact`, `_compact`, `_wake_after_compact`, `_safe_tail`), `src/litetui/turn_engine.py`
(`autocompact_due`, `compact_request`), `src/litetui/conversation.py` (`record_truncate`),
`src/litetui/prompts/compact.md`, `src/litetui/prompts/wake-after-compact.md`. The mechanism is
sound (persist to the store first, replayable truncate record, resume ping; recovery was clean both
times). The items below are what the measurement showed.

- [ ] **`_safe_tail` keeps NOTHING during a tool loop** (`app.py` `_safe_tail`, `compact_keep_recent`
      default 4). It trims the tail forward until it starts on a `user` message, to keep tool-call
      pairs valid for LM Studio; in an agent loop the last 4 messages are assistant/tool pairs, so the
      tail empties and the whole conversation is summarised. Both compactions kept zero verbatim
      (`keep_from` 69 of 69, then 209 of 209): the last tool result — the compile-error list the seat
      was mid-fix on — was gone and had to be re-derived (5 probes / 30 s, then 2 / 20 s). Fix: treat
      `want` as a minimum, and walk BACK to the assistant message that opens a complete tool round
      (an assistant carrying `tool_calls` whose results all sit inside the tail), or to an assistant
      without tool calls — either satisfies the pair constraint and keeps the current round. Arm:
      a conversation ending in 4 assistant/tool pairs must keep ≥ 1 complete round after compaction.
- [ ] **The truncate record carries no measurements** (`conversation.py` `record_truncate`; the
      `_compact` worker already holds `before_chars`, `before_count`, the STEP 1 `writes` list and
      `auto`). Add: chars/tokens before and after, `duration_s` of the compaction turn, summary
      length, the store files written, auto vs manual, and `ctx_used` at trigger (from
      `_autocompact_due`'s `pct`). Today none of this is in the jsonl: the 242 s second compaction and
      its three store writes were only inferable from file mtimes, and "50 % after the first compact"
      cannot be reconstructed at all.
- [ ] **Message rows carry no token usage.** Record the stream's usage block (prompt/completion
      tokens) on each assistant `msg` row so the context fill curve between compactions is replayable
      per model. Cheap; the numbers already arrive in the response.
- [ ] **Measure where the ~4 minutes go** (compaction turn #2: last message 00:38:03 → truncate
      00:42:05, three store writes inside it). Prefill of the head dominates at this size, and each
      STEP 1 tool round re-sends the head — log per-round timings on the compaction card before
      deciding between a lower auto threshold, a smaller head, or relying on the server's prefix
      cache. Compaction is `@work(exclusive, group="chat")`, so the seat is blocked for the whole turn.
- [ ] **Summary #2 opened with "Store persisted. Now the summary:"** against `compact.md`'s "Do not
      describe what you wrote to the store". Either strip a leading store-status line from the
      returned summary or reword the prompt so the two steps do not invite a transition sentence.
      Minor; costs a line of context every compaction.
- [x] Not a defect, keep: the wake ping (`wake-after-compact.md`, user role) makes the model re-verify
      state with 2–5 probes before resuming. That is the right behaviour and cost 20–30 s each time.
