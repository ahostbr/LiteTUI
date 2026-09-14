# LiteTUI — TODO

One line per item, newest section at the top. The portfolio board (`C:/Projects/scripts/portfolio.py`)
reads the first open line of this file, so keep the next thing to do at the top.

## Compaction — completed 2026-09-14

Original observations: 27B seat, 2026-09-06, conversation
`.convos/83134335-5797-47e3-a6c7-1f0d3d51a4cf/convo.jsonl`.

- [x] Preserve recent tool rounds: `_safe_tail` treats the requested count as a minimum and walks backward to a valid boundary, retaining complete parallel tool-call/result groups. Zero still disables retention; malformed tails cannot introduce orphan results.
- [x] Persist compaction measurements on the truncate row: before/after characters and message counts, reported trigger tokens/context percent, explicitly labelled token estimates, duration, summary length, model, successful store-write paths, auto/manual mode, and per-round usage/timings.
- [x] Persist each streamed assistant request's provider usage and model on its `msg` row. Metadata stays outside model messages; requests without usage do not inherit stale counts.
- [x] Show and retain per-round model-request, first-chunk, and tool-execution timings on the compaction card, also saved in the truncate measurements. High-resolution durations distinguish fast calls. First-chunk latency includes transport/server work; it is not a pure prefill measurement.
- [x] Reword the summary prompt to start directly with task/state and explicitly prohibit store-status announcements or transition sentences.
- [x] Preserve the existing optional wake ping and its real-input/failure/abandoned-turn safeguards.

Validation: compaction, wake, and streamed usage checks: 31 passed after the final
clock fix. Broader related checks: 311 passed; the timing assertion was subsequently
fixed and passed above. Six footer-field failures also reproduce with the saved
pre-change app and are unrelated to these TODOs. Ruff adds no diagnostics.

Post-compaction token estimates use chars/4, excluding request tool schemas and live
store additions; actual tokens are available on the next provider usage row. No live
27B performance rerun was made, so the historical four-minute delay is not claimed
resolved or remeasured. New runs now capture the evidence needed to diagnose it.
