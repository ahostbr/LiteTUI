# OpenBolt handoff — 2026-09-10/11, T564-B through T583

Seat `c30dbfa8-292c-46a7-81b9-7cc4c88b177f`, LiteSuite worktree
`.worktrees/openbolt-t407-t279`, LiteTUI worktree `.worktrees/openbolt-litetui`.
Written mid-T579 so a successor can continue without re-deriving. Every row
names a sha, a symbol, or a re-runnable command.

## Landed and merged

| card | where | merged at |
| --- | --- | --- |
| T564-B close-out | LiteSuite `b937675a0` | `develop 7faf33536` |
| T565 self-heal wait | LiteSuite `d16c68d1f` | `develop b753d26ba` |
| T576 PickDouble backend | LiteTUI `11203f1` | `main ccf0229` |
| T573 pieces 1–3 | LiteTUI `f75a9cc`, `259c8a3`, `cc8aeb5` | `main 877af99` |
| T578 footer cell ownership | LiteTUI `5676858` | `main ea35989` |
| T583 /settings could not save | LiteTUI `e00e75d` | `main 27a201c` |
| T579 part 1 (4 causes) | LiteTUI `f767045` `89a9a9c` `11130ba` `83074a3` | `main 56e60c5` |
| T585 delete Seat.rebind | LiteTUI `2550047` | `main 7d7442e` |

✅ **T565's scope paragraph is CLOSED.** I shipped it saying the mechanism
was proven but the full-run failure was NOT proven gone, and that only the
gated suite could settle it. Ryan ordered that run: desktop full suite on
develop `fb9d09c9d`, 32 s wall, 267 files, and
`retention-selfheal.integration.test.ts` is **2/2 green inside the full run**.
The original failure is gone at real load. 266/267 files passed; the one red
is `command-reply-falls-back-to-edge.test.ts` x4, pre-existing since
`2bf3f5f9f`, carded T593 and not mine.

**Nothing else above is verified by Ryan.** T573 in particular needs his hands:
Ctrl+P, click the footer "☰ commands" button, arrow to the footer and press
Enter on plan, type `/plan`. T583 needs him to save settings once.

## In flight — T579 part 2

Branch `fix/t579-litetui-reds-2`, off `origin/main 7d7442e`. Part 1's four
causes are merged at `56e60c5`; these two are pushed and unmerged:

- `3bb63de` — `tests/test_cli_model_flag.py` 5 red → 5 passed. One line:
  `asyncio.get_event_loop()` raises on 3.12+, so every arm died in the
  harness before reaching `_apply_cli_args`. Now `asyncio.run`.
- `92f55c4` — `tests/test_studio_tool.py` + `tests/test_tools_registered.py`,
  2 red → 22 passed. One cause: `7b6640f` made studio/subagent/listen/
  pccontrol DEFERRED (`DEFAULT_DEFERRED`, plugins/tool_search.py:26) —
  withheld from the offer, still dispatchable. The arms now assert the
  contract (declared AND absent), because absence alone cannot tell
  "withheld" from "lost".

### Still open — 8 failures, 5 files

`test_tool_policy_wiring.py` (3) · `test_tools_disabled.py` (2) ·
`test_runtime_log_producers.py` (1) · `test_theme_extra_tokens.py` (1) ·
`test_tool_schemas.py` (1). Full list and error classes: SilverBolt's msg
`d5677761` — read it in full, the notification truncates it.

### The environmental question, PARTLY ANSWERED and I was wrong

I argued the 5 `no current event loop` failures were probably a py3.14
worktree artefact and pressed twice for a run in the primary clone before
chasing them. **They were not environmental.** `asyncio.get_event_loop()` is
wrong on every modern Python; the 3.11 clone passes only because it is
behind, and will fail identically when it moves. Filing them under
"environmental" would have parked a real defect behind a permission request.

The question stays genuinely open for the REMAINING files, where the suspect
is different: `paths.ROOT` and `settings.settings_path` resolve from the
PACKAGE location, so a worktree differs from the primary clone. `972d358`
raised it and it is still not established. Worth one run there before
chasing the last five files — but do not assume, as I did, that a plausible
environmental story survives reading the code.

## Traps measured today, all still live

- **`tests/` holds two mutually hostile styles.** A module-level `sys.exit()`
  means script-style: naming it to pytest aborts collection with INTERNALERROR
  and reports "no tests ran" — a failure of EVERYTHING, not that file.
  `tests/run_all.py:1-20` documents the discriminator. `test_footer.py`,
  `test_harness_tool.py`, `test_seat_identity.py` are script-style.
- **A patch script silently converts CRLF to LF.** `io.open(p, encoding="utf-8")`
  reads in text mode; writing back with `newline=""` emits LF. `git diff` shows
  nothing, `git status` shows the file modified. Pass `newline=""` on the READ
  too. Pattern `ee77b5a4…-1789087375`.
- **`git bisect` returns *a* boundary, not *the* boundary.** On T573 it named
  `ac4bf71` (19 days early, touches no palette file, own body says "935
  passed"). The arm had broken, been fixed, and broken again at `9660da1`.
  Verify the returned commit explains the CURRENT failure.
  Pattern `ee77b5a4…-1789087363`.
- **Textual docks OVERLAP at the same edge**; they do not stack. `.ctx-label` is
  `Region(x=0, width=190)` and covers the whole footer row. The palette button
  is clickable only because `ContextFooter` composes it LAST. `width: auto` does
  NOT fix this — measured, the label becomes `Region(x=103, width=87)` and still
  covers the button at 176..190. `5676858` pins the satisfiable property
  instead: every clickable footer widget owns its own cells.
- **The Rule 13 hook is a TEXT gate.** A compound command mentioning `tests/`
  reads as a suite run and is refused. Name the files in their own command.

## The through-line

Three times today an arm I wrote was green and worthless, and none was caught by
reading:

1. T573 piece 1 — `width: auto` was covered by nothing; the mutation said so.
2. The arm I then added for it read geometry after ONE pause and went flaky —
   the T565 trap, written by me about an hour after I landed the fix for it.
3. T578's carded fix and carded invariant were both wrong, and I had written
   the card myself from a number I had misread as a property.

And one more shape worth carrying, from `f767045`: an assertion loose enough to
be satisfied by a DIFFERENT failure is not a weaker test of the same thing, it
is a test of something else. `chk("self-send is refused", out.startswith("[error]"))`
was green while never reaching the self-send guard, because the resolve error is
also `[error]`. Deleting that guard used to kill one arm; it now kills three.

**Awareness does not execute.** What caught all of these was structural: run the
check against a known-bad state and confirm it goes red, every time, before
believing a pass. On T583 the same rule picked the instrument: the AST arm can
say the control rows exist, only the live probe can say the screen saves.
