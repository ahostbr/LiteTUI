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

**Nothing above is verified by Ryan.** T573 in particular needs his hands:
Ctrl+P, click the footer "☰ commands" button, arrow to the footer and press
Enter on plan, type `/plan`. T583 needs him to save settings once.

## In flight — T579

Branch `fix/t579-litetui-reds`, rebased onto `origin/main 27a201c`. Two commits
done, both test-only:

- `f767045` — `tests/test_harness_tool.py` 27/30 → 30/30. Stale since `4dc8a36`
  (T536) put `resolve_agent` ahead of every send check; fixtures use ids no
  registry holds. Fixed by handing the module a temp `AGENTS_DIR`, the idiom
  its own `check` section already uses for the maildir.
- `89a9a9c` — `tests/test_seat_identity.py` 24/25 → 26/26. Reversed by
  `f64442b` (T507-T5): the seat id is process-stable now and
  `_sync_seat_identity` is a deliberate no-op.

### What remains, from SilverBolt's list (msg `d5677761`, read it in full)

14 files, 30 failures, measured at `origin/main ccf0229`. Re-run:
`.venv/Scripts/python.exe -m pytest <the 14 files> --tb=line -q --no-header`

Still open after my two commits and T583:

| file | n | note |
| --- | --- | --- |
| `test_cli_model_flag.py` | 5 | all `RuntimeError: no current event loop` |
| `test_seat_rebind.py` | 6 | 3 AttributeError on SimpleNamespace doubles |
| `test_seat_convo_identity.py` | 3 | same T507-T5 reversal as `89a9a9c` — likely one cause |
| `test_tool_policy_wiring.py` | 3 | AssertionError |
| `test_no_dead_controls.py` | 2 | now PURELY `tool_auto_background_s` (T517) |
| `test_tools_disabled.py` | 2 | |
| `test_runtime_log_producers.py` | 1 | |
| `test_studio_tool.py` | 1 | "spec exists but never reaches the model" |
| `test_theme_extra_tokens.py` | 1 | |
| `test_tool_schemas.py` | 1 | files in `tools/` loaded by no tool |
| `test_tools_registered.py` | 1 | tools not offered to the model |

`test_command_palette.py`'s one red was T573 and is fixed.
`test_settings_controls.py` and `test_settings.py` were T583.

### The open question I would answer FIRST

**How many of these are environmental?** This worktree runs Python 3.14 and
`paths.ROOT` / `settings.settings_path` resolve from the PACKAGE location, so it
differs from the primary clone. `972d358`'s body raised this and it is still not
established. The 5 `no current event loop` failures are a 3.12+ asyncio change
(`get_event_loop` no longer creates one implicitly) and are an interpreter
artefact until proven otherwise. One run of the 14 files in `C:/Projects/LiteTUI`
splits real from environmental and could remove a third of the list. I asked
Sentinel for the go-ahead; it had not arrived when this was written.

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
