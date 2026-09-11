# SilverBolt — LiteTUI evening, 2026-09-10

Seat `1ccbc1d5-e16b-4022-b42e-8aa9659028c6`, tree
`C:\Projects\LiteTUI\.worktrees\seat-mcp`. Written before a compaction, at
Sentinel's instruction. Every row names a sha, a symbol or a re-runnable query.

## 0. What landed, and what is UNVERIFIED

| card | sha | state |
| --- | --- | --- |
| T570 — footer chips, nav, both panels | main `5f64ec7` | merged · **VERIFIED by Ryan 20:5x on a live Codex-OAuth seat** — the only thing today with his eyes on it |
| T571 — `tasks.list`/`kill` over `--rpc` | main `7e07b08` | merged, unverified |
| T572 — no keyboard dialog headless | main `5df7d5b` | merged, unverified |
| T558-C — the one runner measures every file | main `a514f1e` | merged, unverified |
| T580 — the guard rule + the lint debt | `fix/t580-script-guard-coverage` `e7b408b`, `f795e63` | pushed, NOT merged |

## 1. 🔴 A DEFECT I SHIPPED AND MIS-ATTRIBUTED — read this first

`test_settings_controls.py::test_every_settings_field_has_a_control_or_is_exempt`
fails on **merged main**:

```
these Settings fields have no control, so the settings screen cannot save
AT ALL: footer_show_bg, footer_show_subagents
```

`git log -S footer_show_bg -- src/litetui/settings.py` → **`0d099bc`, T570
piece 1, mine.** I added two `Settings` fields and no control rows in
`settings_screen.py`.

⚠️ **AND I REPORTED IT AS NOT MINE, TWICE** — in `dc4765e`'s body and to
Sentinel in `71401be7`, as "30 pre-existing failures, none in files I touched".
Both halves of that sentence were true and the CONCLUSION was false: the
failing FILE is not one I edited, and the CAUSE is a field I added. **"I did
not touch that file" does not establish "that failure is not mine."** The
correction went to OpenBolt as `d5677761` before he started T579.

⬜ **NOT VERIFIED: whether "cannot save AT ALL" is literal.** It is the
assertion author's wording. If literal, merged main cannot save settings and it
is Ryan-facing. `test_no_dead_controls.py` and
`test_settings.py::test_every_field_is_reachable_without_opening_its_tab` also
name these two fields MIXED with `tool_auto_background_s` (T517, not mine), so
those two need per-field attribution rather than a blanket call.

**The fix is one of two, both named by the assertion itself**: add a row per
field in `settings_screen.py`, or add the names to `_collect`'s exempt tuple
with a comment saying which editor owns them. The footer chips are toggled from
`/settings`' footer section, so a control row is the honest one.

## 2. T580, pushed, not merged

- `e7b408b` — SIM102 at `run_all.py:104` collapsed; the twin at `:101` LEFT
  ALONE with a comment saying why (it is followed by an `elif`, so collapsing it
  would change the classifier). Both `PLW1510` `# noqa`'d with the reason:
  `check=True` would abandon the run at the first failing file.
- `f795e63` — `tests/test_script_guard_coverage.py`, the rule
  **guarded ⟺ constructs a `LiteTUI()`** (4 of 4 and 10 of 10, measured),
  forward arm, reverse arm as its negative control, count pinned at 4, and a
  positive+negative control on each AST detector.

🔴 **THE CARD'S PREMISE WAS MINE AND WRONG.** I reported "10 of 14 script-style
files run with no protection". That was an IMPORT COUNT plus a keyword scan.
Measured by running all 14 with the three stores snapshotted and restored, in
both environments: **none of the ten touches any of them.** The scan's hits were
a FakeApp attribute called `registered`, a test's own `tempfile.mkdtemp`, and a
file that only reads source TEXT. The probe was self-checked against a synthetic
writer and caught it, so the negative is a measurement.
Probes: `scratchpad/probe_t580_writes.py`, `_bare.py`, `_control.py`,
`_selfcheck.py`.

## 3. T581 — QUEUED, NOT STARTED

Ryan's screenshot (20:5x): footer reads `bg:1` while the Background panel reads
`(0) Nothing running in the background`, right after task `t-cc94ad` (a 300s
timeout) finished and its inbox line was delivered.

Both surfaces already read `tasks.split_live` — chip at `app.py`
`ctx_label_text`, panel at `task_screens.py` `BackgroundProcessesBody.rows`. So
the likely cause is NOT two sources but **a missing recompose**: the panel's
`sync()` rebuilds on a membership change, and nothing re-renders the FOOTER when
a task leaves the live set. `_refresh_ctx_label` is called by the nav methods and
by the settings path — check whether `_run_background`'s completion calls it.
Reproduce with a task that times out.

## 4. Laws this evening paid for, each with its measurement

1. **A measurement of what ARRIVES is not a measurement of what was SENT.**
   I concluded "the provider sends no join key" from reading three of our own
   files; the CLI sends `tool_use_id` on all three task messages and our handler
   dropped it. Pattern `1ccbc1d5-…-1789085615`.
2. **A log line that precedes the write it describes is not evidence the edit
   landed.** My patch helper prints `ok` per replacement and writes once at the
   end, so a later MISS discards six edits that logged as applied. Pattern
   `1ccbc1d5-…-1789086233`.
3. **Upgrading an instrument's precision does not widen its scope** — and the
   upgrade is when scope stops being questioned. `run_all`'s AST check was the
   careful one and missed 83 tests that `conftest`'s blunt substring caught.
   Pattern `1ccbc1d5-…-1789086911`.
4. **A guard for a change in file A can live in a file named after B.**
   `test_plugin_dogfood` went red at `6b25437` and the footer neighbourhood
   could not contain it. Pattern `1ccbc1d5-…-1789082074`.
5. **A delete-equivalent probe can be red for a reason that is not the code.**
   Three gates now, none of them the exit code alone: marker landed, file still
   `ast.parse`s, run produced OUTPUT.

## 5. How to resume

1. Read this file and §1 first — the shipped defect outranks the queue.
2. Keep id `1ccbc1d5-e16b-4022-b42e-8aa9659028c6`; arm ONE watcher with the
   explicit `--agent-id` form, never `watch-auto`.
3. Report "back" to `2dc57f3e-a914-4d1e-b414-36db80e3006a` by **inbox**.
4. T581 is queued (§3). T580 awaits merge. T577 (tool approval over `--rpc`
   routed to the host) waits on Ryan's priority.
