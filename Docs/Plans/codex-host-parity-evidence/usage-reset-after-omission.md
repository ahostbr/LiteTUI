# Native usage reset after omitted counters

Task: REL-20260914-CODEX-PARITY

NativeUsage compared cumulative counters only against the immediately previous
snapshot and the turn baseline. An intervening notification with absent counters
discarded the prior observation. In a fresh turn, a subsequent reduced total could
then be reported as valid turn usage instead of an unknown aggregate after reset.
Partial omissions caused the same defect for individual counters.

The meter now retains the last observed value of each supported cumulative counter
for reset detection. Missing counters remain missing in the displayed snapshot;
historical values are never substituted for current usage. Any detected decrease
keeps the turn aggregate unknown while latest context occupancy remains available.
The retained map is bounded by the six supported counter fields.

Two new regressions reproduced the defect before the fix: a complete omission
between two totals and an output-counter omission while input continued growing.
A control case confirms ordinary cumulative growth remains reportable after an
empty notification. Existing resume, duplicate, context/cache separation and reset
checks continue to pass.

Validation: 18 tests passed across test_codex_usage.py, test_codex_app_server.py
and test_codex_compaction_ui.py; scoped Ruff passed. This is synthetic protocol
coverage, not a new live compaction measurement. The previously tested wheel at
982585a predates this production change. Full-plan acceptance remains open.
