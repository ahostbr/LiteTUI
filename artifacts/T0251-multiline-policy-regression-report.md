# T0251: multiline shell policy regression evidence

## Disposition proposed for review

**The original classification bug is superseded on the current base by T0116.**
No production fix or mechanical `re.MULTILINE` addition is warranted. Ember
explicitly selected tests-only resolution. Orchestrator and Owner own final card
and merge disposition; this report does not declare Done.

- Measured base: `8a423871ece357464f2fe7eadcd01e86daa40ee6`.
- Own worktree: `C:/ExampleProjects/.scratch/T0251/LiteTUI`, branch `ember/T0251`.
- Regression implementation: `e64af6d926389e757bd9a8cc53020cbd84dc19b2`.
- Line-ending normalization: `357b4c8` (no behavior change).
- Fixing ancestry: `git merge-base --is-ancestor 1a34e39 HEAD` exited **0**.
- T0116 merge `1a34e397e3d980f5f4432a97d83d70aa9fbd78ad` includes child
  `1b317d8`. Inspection of that child's actual `tool_policy.py` confirms
  `deny_floor.shell_commands`, iteration over each part, and normalized argv
  `pattern.match(view)`. Current implementation is at
  `src/litetui/tool_policy.py:791,808,837-840`.

## Original reproducer and both polarities

`echo a\nrm -rf X` (an actual newline) already returns `deletion` from
`danger()` on the unchanged base. Every supported profile retains
`destructive_irreversible` in its classified capabilities. At a neutral/main
workspace, strict and interactive **CONFIRM**; autonomous **ALLOW** is its
intentional no-approval authority, not a classification miss.

Inside the seat's own temporary linked worktree, interactive **ALLOW** remains
the T0246 confinement exemption; classification still reports deletion. Strict
still confirms, and autonomous still allows. Quoted inert multiline arguments,
e.g. `echo 'a\nrm -rf X'`, remain ordinary and are not promoted into deletion.
Strict still confirms ordinary process execution by explicit human-selected
supervision; interactive and autonomous allow it.

`tests/test_tool_policy_multiline.py` adds **166** parameterized cases through
public `danger`, `classify_shell`, and `evaluate` behavior. Coverage includes
LF/CRLF, execution separators, four danger classes, inert quoted newlines,
per-segment flags, executable wrappers/substitutions, escaped-newline
continuations, and own/main worktree profile distinctions. Commands are policy
inputs only: no deletion, extraction, or system command is executed.

## Checks actually run

| Command / scope | Result |
| --- | --- |
| Baseline `python -m pytest tests/test_danger_table.py -q` | **497 passed**, including the card's reported venv-path test |
| Final `python -m pytest tests/test_tool_policy_multiline.py -q` | **166 passed** in 4.36s |
| `python -m pytest tests/test_tool_policy_multiline.py tests/test_danger_table.py tests/test_destructive_floor.py tests/test_worktree_scope.py tests/test_interpreter_policy.py -q` | **1493 passed** in 39.27s |
| `python -m pytest tests/test_tool_policy.py tests/test_tool_policy_wiring.py tests/test_destructive_floor.py tests/test_worktree_scope.py tests/test_interpreter_policy.py -q` | **845 passed, 2 failed** in 34.69s; unrelated existing contract failures below |
| Exact two failing node IDs rerun independently before review scope expansion | **2 failed** on unchanged tracked files |
| After review correction: `python -m pytest tests/test_tool_policy.py::test_argument_sensitive_desktop_and_harness_actions tests/test_tool_policy_multiline.py -q` | **167 passed** in 11.09s |
| After review correction: `python -m pytest tests/test_tool_policy.py tests/test_tool_policy_wiring.py tests/test_tool_policy_multiline.py tests/test_danger_table.py tests/test_destructive_floor.py tests/test_worktree_scope.py tests/test_interpreter_policy.py -q` | **1509 passed, 1 failed** in 75.66s; only the T0251-A fixture failure remains |
| AST parse of the new test file | **PASS** |
| `git diff 8a42387 HEAD --check` after line-ending normalization | **PASS** |
| `python -m ruff --version`; `python -m mypy --version` | **Unavailable**: neither module installed in current interpreter; no installation/download attempted |

No full suite was run. Logs:

- `C:/ExampleProjects/LiteTUI/output/tasks/t-8ee4e2a0d8fe4b20a80eb7788d0789ce.log`: baseline 497 passes. Its subsequent standalone Python probe initially failed to import `litetui`; rerunning with `PYTHONPATH=src` resolved the probe environment, without changing code.
- `C:/ExampleProjects/LiteTUI/output/tasks/t-4ab886822d564fc69de2b9d1ce2ccea1.log`: combined 1493 passes and ancestry exit 0.
- `C:/ExampleProjects/LiteTUI/output/tasks/t-7a0cee432b0b4711b17c0012258bb337.log`: initial broader 845 passes / 2 failures.
- `C:/ExampleProjects/LiteTUI/output/tasks/t-de8d8ba43e1f47fb96daf66636b6cbfc.log`: after scope correction, touched expectation + multiline 167 passes; complete focused affected set 1509 passes / 1 remaining fixture failure.

## Review-requested scope correction and remaining T0251-A

Both initial failures were reproduced while all tracked production and existing
test files matched the base; only the new multiline regression file was untracked.
Orchestrator subsequently expanded T0251 narrowly to fix the stale launch expectation.

- **Corrected within T0251:**
  `tests/test_tool_policy.py::test_argument_sensitive_desktop_and_harness_actions`
  now expects ordinary pccontrol launch **ALLOW** without a destructive
  capability, updates its stale docstring, and explicitly retains strict launch
  **CONFIRM**. This aligns tests with T0116; production behavior is untouched.
- **Remaining follow-up only:**
  `tests/test_tool_policy_wiring.py::test_a_seat_s_own_worktree_command_runs_without_a_modal_but_a_stranger_s_does_not`:
  `SimpleNamespace(name=...)` fixture lacks `current_spawner()`, required by
  `approval_relay.py:124`. Follow-up should use a contract-compatible seat
  fixture returning no spawner, preserving stranger confirmation coverage.

Linked card **T0251-A** was updated to cover only the fixture failure, queued,
tier/thinking unset with a needs-decision note. Owner must choose both settings.
No follow-up fixture implementation or production code changed.

## Scope and verification limits

- **Implemented:** tests and this evidence report only; no production policy,
  deny-floor, settings, or parser changes.
- **Tested:** current repository artifact through pure policy and focused
  affected-contract tests, with explicit failures/unavailable tools above.
- **Running-product verified:** **NO**. No editor, live app restart/rebuild,
  UI approval path, model load, merge, or push was performed.
- Card remains **reviewing**, awaiting leader review, Orchestrator gate, and human
  intent. No task completion call was made.
- Tool-generated `tests/test_tool_policy_multiline.py.lock` is untracked and
  excluded from commits; it was not deleted or treated as source.
