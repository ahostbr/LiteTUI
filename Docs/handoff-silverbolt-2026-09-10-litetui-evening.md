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
| T580 — the guard rule + the lint debt | main `1a0aa51` | merged, unverified |
| T581 — the chip repaints when the task set moves | main `9921a48` | merged, unverified · **two of its four arms are NOT pinned by the fix**, see §3 |
| T583 — my T570 defect, landed by OpenBolt | main `27a201c` | merged · **§1 RED CONFIRMED GONE**: `pytest tests/test_settings_controls.py` → 3 passed on `29a8227` at 23:0x |
| T582 — a skill body names where it was loaded from | main `d040e7a` | merged, unverified |
| T592 — the suite stops writing the live task store | main `28004e6` | merged, unverified · carries the T581 flake fix as its own commit |
| T577 half 1 — approval over `--rpc` | main `3794261` (+ ruff fold `afdb19b`) | merged, unverified |
| T577 half 2 — the card in Frontier Chat | LiteSuite develop `c5dee454a` | merged, unverified · **T577 stays REVIEWING: the end-to-end is Ryan's to see** |
| T587 — a failed workspace save is not silent | LiteSuite develop `ef7a5336d` | merged, unverified |
| T593 — the electron double catches up | LiteSuite develop `46b786921` | merged, unverified |
| T579 remainder — the last two LiteTUI reds | main `b13bada` | merged, unverified · **one red was hiding a real leak** |
| T590 — the installed Codex wrapper | 7 installed files, NOT in git | REVIEWING · §3e has every path + stamp · **wake proof moved to Ryan** |

## 1. 🔴 A DEFECT I SHIPPED AND MIS-ATTRIBUTED — FIXED, read it for the lesson

✅ **THE RED IS GONE FROM MAIN.** `27a201c` (T583, OpenBolt's `e00e75d`) added
both control rows; `11130ba` (T579 part 1) taught the dead-control detector the
getattr channel. Re-run to confirm:

```
.venv/Scripts/python.exe -m pytest tests/test_settings_controls.py -q -p no:cacheprovider
```

→ **3 passed** (measured 23:0x on `29a8227`, which has `27a201c` as an
ancestor). `src/litetui/settings_screen.py:611,615` now carry the two rows.
Sentinel flagged this row as stale in `16eac249`; I re-ran it rather than take
the report, and he was right.

⚠️ **"cannot save AT ALL" WAS LITERAL — that question is now answered, and the
answer came from the FIX, not from me.** I left it marked NOT VERIFIED below.
The commit that repaired it is titled *"the settings screen could not save AT
ALL — two fields had no control"*. So for the hours between `0d099bc` and
`27a201c`, merged main could not save settings, and it was Ryan-facing.

### What it was, and why the row stays

`test_settings_controls.py::test_every_settings_field_has_a_control_or_is_exempt`
failed on **merged main**:

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

✅ **RESOLVED (was: "NOT VERIFIED whether 'cannot save AT ALL' is literal").**
It was literal — see the top of this section. Of the two fixes the assertion
itself named, `e00e75d` took the honest one: a control row per field, not an
exempt-tuple entry. `test_no_dead_controls.py` and
`test_settings.py::test_every_field_is_reachable_without_opening_its_tab` named
these two fields MIXED with `tool_auto_background_s` (T517, not mine) — that
mixing is why a blanket call would have been wrong, and per-field attribution
is what found the real owner of each.

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

## 3. T581 — FIXED on `fix/t581-bg-chip-panel` `063d64d`, pushed, NOT merged

CONFIRMED, and it was not two sources. `_save_background()` — the one place
every transition already passes through — now also calls `_refresh_ctx_label()`.
The chip was never wrong, it was never ASKED: `ctx_label_text` is a property
that recomputes on call, and nothing called it when `bg_tasks` moved.

⬜ **WHY IT LOOKED INTERMITTENT** (`widgets.py:808-818`): the footer RECOMPOSES
and its compose re-reads the property, so any unrelated activity silently
corrects the chip. Only a seat that fires a long task and then WAITS ever sees
it — the 300s timeout in Ryan's screenshot.

🔴 **A SECOND, OLDER DEFECT FOUND BY THE THIRD ARM**: `_kill_background` set
`KILLED` in memory only, so the store still said `running` and `tasks.load`
marked it **LOST** at the next boot — a deliberate stop reported as "gone with
the app". The same `_save_background()` call fixes both halves.

⚠️ **TWO OF THE FOUR ARMS ARE NOT PINNED BY THE FIX** (their probes came back
green), both for that same recompose reason — `notify()` in the kill path
triggers one, and once anything recomposes the property and the painted label
agree. Labelled in the commit body. Do not read four greens as four proofs.

### The original note, kept because it was right

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

## 3b. T582 — the ls-mark path, and what the card had backwards

`fix/t582-ls-mark-skill-path` `fe6d2d8`, pushed, NOT merged.

🔴 **THE CARD BLAMED THE SKILL TEXT; NO SKILL.md NAMES A REPO PATH.** The literal
`C:/Projects/LiteTUI/skills/ls-mark/mark.py` exists in exactly one file on this
machine — the Codex seat's own `convo.jsonl` — because the seat INFERRED it from
the only skills directory the system prompt named. Re-run:

```
grep -rIl "LiteTUI/skills/ls-mark" C:/Projects/LiteTUI C:/Projects/liteharness-oss
```

The real defect: `skills.load()` returned SKILL.md **verbatim**, so the two
placeholder spellings in the wild (`${CLAUDE_SKILL_DIR}`, `<this skill's
directory>`) bound to nothing. Measured across the discovered library:
**25 skills, 100 occurrences, all unbound → 0 after.** `bind_skill_dir` in
`skills.py` substitutes both and prepends `Base directory for this skill:`.

⬜ **NO liteharness-oss COMMIT, DELIBERATELY.** The card named it as the second
repo. With the loader binding both spellings, ls-mark works unchanged, and
Claude Code injects a base directory anyway — so an OSS edit would be 25 files
of text churn against a pre-commit PII gate to work around one missing line in
the host. Flagged to Sentinel as a premise correction, not silently skipped.

✅ **ONE CARD ITEM WAS ALREADY BUILT AND I NEARLY REBUILT IT**: "precedence when
two cached versions exist". `skills.resolve_roots` already sorts glob matches by
mtime; with 1.0.15 and 1.0.16 both on disk it returns only `.../1.0.16/skills`.

⚠️ **ONE OF THE EIGHT ARMS IS PINNED BY NEITHER PROBE**
(`test_an_unknown_name_is_not_given_a_base_directory`) — it guards the ERROR
path against acquiring a header, a mistake not yet made. Forward guard, not a
proof. The census arm sees only the two spellings that exist today.

⬜ **UNTRACKED `background-tasks.json` IN THE REPO ROOT** — a killed `t-343839`
"sleep 300", written by my own T581 arms: `_save_background()` resolves relative
to cwd. Test pollution, not a shipped defect, and NOT in `.gitignore`. Reported,
not patched — a `.gitignore` line would hide it rather than fix it.

## 3c. T577 — approval over `--rpc`, half 1 of 2

`fix/t577-approval-over-rpc` `e6decdd`, off main 28004e6, pushed, NOT merged.
Ryan (a-456cfd80): *"i want the approvals to route threw frontier chat GUI"*.

Wire: `tool_approval_requested` out (id, tool, input, profile, why,
`timeout_s`), `{type:"approve", id, allow, remember?}` in. The branch is at
`app._execute_tool`'s CONFIRM; `side_panel.show_dialog` is UNTOUCHED — it is
generic and serves three callers with no approval semantics.

🔴 **`None` AND `DENIED` ARE DIFFERENT ANSWERS.** Both refuse; only one is a
person. A new `no-host` refusal says the approval went unanswered, because
`[policy denied by user]` would tell the model someone decided and a broken
host would look like a settled decision forever.

⬜ **TIMEOUT 300s, MEASURED**: the adapter's `COMMAND_TIMEOUT_MS` (60s) governs
an RPC command with no human on it; `user_input_requested` waits forever. It
rides on the wire so the host can show a countdown, and is read at CALL time.

⚠️ **NOT DONE, NOT CLAIMED**: the pilot arm driving a REAL `litetui --rpc`
CHILD PROCESS. The arms use a real `LiteTUI` object with `_rpc` set (T558-A's
own precedent, test_ask_over_rpc.py:183), so they prove the object routes to
the wire, not that a spawned child does. It belongs with half 2.

**HALF 2, NOT STARTED** — LiteSuite `feat/t577-approval-card` off
`origin/develop`: `LiteTuiAdapter.ts` forwards the request as an activity;
Frontier Chat ALREADY has an approval flow for claudeAgent tool permissions —
REUSE that component, do not fork it. The click sends `approve` back.

## 3d. T579 remainder — a gate red for one reason hid a real defect

main `b13bada`: `e8bb28c` (the leak + the census) and `a1e62a4` (the schema scan).

🔴 **THE COUNT WAS NOT THE DEFECT — IT WAS WHAT HID ONE.**
`test_runtime_log_producers.py` failed at `assert len(calls) == 18` (line 101)
and so never reached line 109, where T499's producer says:

```
runtime_log.record("task.started", task_id=task.id, tool=name, label=task.label)
AssertionError: assert not ({'label','task_id','tool'} & PROHIBITED)
```

`label` is in `PROHIBITED` **by name**, and `tasks.label_of` (tasks.py:96)
returns the first 60 characters of the call's `command` or `prompt`. Every
background tool call was writing the request into the ALWAYS-ON runtime log.
Fixed in production, not the gate.

⬜ **THE REMOVED PRODUCER WAS CHECKED, NOT WAVED THROUGH**:
`harness_rebind_failed` is gone because `f64442b` (T5) removed the rebind path
and T585 deleted `Seat.rebind` — the note survives at `app.py:2290`.

⚠️ **A BLIND SPOT NAMED**: `app.py:1959` records `"task." + task.state`, so
`task.done/failed/killed` reach the log and no constant-scanning gate can
enumerate them. A new arm pins that there is exactly ONE computed producer.

⬜ **`test_theme_extra_tokens.py` IS GREEN** at `afdb19b` — 3 runs alone. It was
on the card's list of three and I could not make it fail. Accepted as green by
Sentinel; if a full run reds it, that is order-dependence and a new card.

## 3e. T590 — the installed Codex wrapper, FIXED OUTSIDE GIT

🔴 **NOTHING HERE IS IN A REPO.** Seven files under `~/.codex/skills/` were
rewritten in place. There is no sha to revert to — the only way back is the
`.bak` files, so their stamps are the record:

| stamp | what |
| --- | --- |
| `20260910_224657` | the 4 scripts (`manual_liteharness.py` ×2, `liteharness_inbox_watcher.py`, `liteharness_watcher_supervisor.py`) |
| `20260910_224903` | `liteharness/SKILL.md`, `liteharness-manual-start/SKILL.md` |
| `20260910_224948` | `ls-liteharness/SKILL.md` |

Every one sits beside the file it replaced, as `<name>.bak.<stamp>`. Nothing was
deleted. `liteharness/scripts/liteharness_notify.py` was already plain and is
untouched; `ls-liteharness/scripts/` is empty.

**The census command, which is the thing to re-run** — it counts ASSIGNMENTS,
not string mentions, because the rewritten files name the dead path in their
docstrings on purpose:

```
grep -rn "liteharness-t370-desktop-wake" ~/.codex/skills | grep -v "\.bak\." \
  | grep -cE "RUNTIME = Path|\$env:PYTHONPATH = |is pinned to"
```

Expected **0**. It read 10-and-more before; my scout's "10 hits, 6 files, two
skills" was a `head -10` reported as a census and MISSED a whole third dir
(`ls-liteharness`).

⬜ **NO OSS CHANGE, DELIBERATELY.** `liteharness/cli.py:520-574`
(`_install_cli_scripts("codex-cli")`) deploys all of these, and all seven
sources carry zero pins; `tests/test_codex_stdout_delivery.py:101` already
asserts byte-identity. The shim was hand-applied over a correctly-deployed
file. ⚠️ A future `install --cli codex-cli` will overwrite the SKILL.md wording
above with the canonical oss text — which is clean, so that is the right
outcome, not a regression.

⚠️ **TWO SIDE EFFECTS I CAUSED, both in the report `4d0b8708`**: running the
installed `check` as a gate CLAIMED my own QUESTION to ArcCap out of `new/`
(repaired — moved back, body verified 2686 chars); and `start --check-now`
registered as `01a08e03`, so that seat's `last_seen` freshness is MY artefact.
It was `[ghost]` ~1h before I touched it.

⬜ **WAKE PROOF NOT MET AND MOVED TO RYAN** (Sentinel, `2a700974`): the
acceptance is his own live Codex Desktop session re-running
`start --check-now` from the installed path and registering. Fire no wake.

## 3e-old. T590 scout, superseded by the above

Scout sent as `83621b39`. Re-runnable:
`grep -rn "liteharness-t370-desktop-wake" ~/.codex/skills` → **10 hits, 6 files,
two skills** (`liteharness` and `liteharness-manual-start`).

🔴 The installed `manual_liteharness.py` is a **bridge shim whose own docstring
says to delete it**: *"Pinned runtime ecb3488. Remove this bridge after verified
T376 deployment."* It `raise SystemExit`s before argparse when
`C:/Projects/.worktrees/liteharness-t370-desktop-wake` is absent — and it is
absent. That is the "exits before bootstrap/check".

✅ **The bridge is also unnecessary**: `liteharness` is an EDITABLE install
resolving to `C:/Projects/liteharness-oss`, so
`importlib.import_module('liteharness.cli_scripts.codex.manual_liteharness')`
already works. The `.bak.20260904_184241` siblings carry the correct
post-cutover form — a plain import, no pin. `manual_liteharness` has **no**
`.bak`.

⬜ **oss needs no code change**: it never had the pin, and
`catalog/skills/ls-liteharness/scripts/` is EMPTY, so the installed files are
not deployed from the catalog. Pure machine-local residue.

⬜ **BLOCKED, deliberately**: the end-to-end wake gate needs a Codex Desktop
seat and the only one is Astra's `01a08e03`, live. Editing the scripts that seat
invokes is the disruption the card warns about. Awaiting Sentinel's call.

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

1. Read §1 first — not for a red to fix (it is fixed, `27a201c`) but for the
   mis-attribution that let it ship: "I did not touch that file" is not
   "that failure is not mine."
2. Keep id `1ccbc1d5-e16b-4022-b42e-8aa9659028c6`. **COUNT watchers before
   arming one** — `Get-CimInstance Win32_Process`, filter on `liteharness`,
   then walk each ParentProcessId up to its `claude.exe` and compare against
   your own `$PID` chain. A surviving watcher and another seat's look
   identical in the process list; only the chain tells them apart. Arm one
   with the explicit `--agent-id` form only if none is yours, never
   `watch-auto`.
3. Report "back" to `2dc57f3e-a914-4d1e-b414-36db80e3006a` by **inbox**.
4. T590 is REVIEWING (§3e) — fixed OUTSIDE git, so the `.bak` stamps are the
   only way back. The live card is T389's command half: the 12 command
   checks in LiteSuite `Docs/VERIFY-PASS-2026-09-05.md`, evidence under
   `~/.liteharness/evidence/verify-pass-2026-09-05/<card>/`. T577 (tool approval over `--rpc` routed to the
   host — the door left unguarded on purpose in T572) waits on Ryan's priority.
   T570 and T569 are the ONLY things Ryan has verified end to end.
