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

## T594 and T584 — added after the doc was first written

| card | where | state |
| --- | --- | --- |
| T579 part 2 (3 causes) | LiteTUI `main 14a173f` | merged |
| T594 headless never loads | LiteTUI `03e572d` -> `main d50f4f5` | merged, UNVERIFIED |
| T584 piece 1, the skill text | liteharness-oss `main 90410bf` | pushed, UNVERIFIED |

### 🔴 THE VRAM INCIDENT, AND THE RULE THAT CAME OUT OF IT

Six `litetui --rpc` probe children of mine JIT-loaded a second 27B into VRAM beside
the one Ryan was running. **NEW HARD RULE, all seats:** no model is loaded by any
path without first confirming none is loaded (`lms ps`) AND explicit approval by
inbox. It is in `~/.claude/CLAUDE.md` under Hard Rules.

Mechanism, measured: LM Studio JIT-loads whatever a **completion** names. Connect is
innocent (`_list_sync` is a REST read). `--model` never loads — it refuses an
unloaded id (app.py:2816-2834). T594 makes a `--rpc` child refuse or substitute so
it cannot load at all; the interactive path still JIT-loads by design (D2/D11,
`_chat_ready_sync`), and an arm exists so nobody widens that later.

### T584 — what is left

Piece 1 (SKILL.md) is pushed. **Piece 2 is the smoke, NOT committed:**
`tests/test_rpc_consult_smoke.py` is UNTRACKED in `.worktrees/openbolt-litetui` —
written, 7/7 green before the incident, already updated to read the resident model
and skip when none is loaded. ~~It has NOT been re-run since.~~

**SUPERSEDED — it was re-run, and it went 4/7.** See the T584 piece 2 row below:
the file now lives at `e2e/consult_smoke_e2e.py`, 8/8, merged on `main 20988af`.

Also still open: `C:/Projects/.claude/consult-config.json` does not exist yet; the
skill reads it relative to the project root and falls back to the template.

~~⚠️ UNMEASURED: codex over `--rpc` (nobody has run one end to end) and llama.cpp.~~

**SUPERSEDED. CODEX IS MEASURED** (2026-09-10 23:1x, and the skill text now says so):
`gpt-6-astra`, answer `ok`, `turn_end` `stopReason: "stop"`, rc 0 on stdin close,
6.5 s — about a third of the LM Studio run. It emits **no `reasoning_delta` at all**,
so a parser that waits for reasoning is wrong on that backend. llama.cpp on 7470 is
still down and still expected to SKIP; re-check with `curl -s -m 3 localhost:7470/health`.

### The shape that caught me four times in one session

A double is a claim about what the subject touches, so it goes stale exactly when the
subject grows a dependency, and nothing links the two. T576 `PickDouble`/`backend`;
T579 two doubles/`_rpc_emit`; T594 every app double/`_rpc` — that last one MINE, an
hour after I wrote the commit body describing it. The fix each time is `getattr` with
a default at the reader, or the double taught the attribute and told why.

---

## Continued — T584 piece 2, then T559 and T588 in other repos

Same seat, later the same night. Three cards, three repositories; this doc is the
index because it is the one a LiteTUI successor opens first.

| card | where | state |
| --- | --- | --- |
| T584 piece 2 | LiteTUI `main 20988af` | merged, **unverified** |
| T584 skill text | liteharness-oss `main 20fe0e7` | merged, **unverified** |
| T559 local timeline order | LiteSuite `develop 13c7d659d` (merge of `e09f15f20`) | merged, **unverified** |
| T588 batch real paths | LiteImage `main bedeff2` (merge of `25d7763`) | merged, **unverified** |

Nothing above is verified end to end. Acceptances outstanding: Ryan running
`/ls-consult` from a Claude seat (T584), his eye on a local turn with tools (T559),
and a LiteImage batch of two yielding two files with a failing item reported failed
(T588).

**T559's own detail lives in the LiteSuite handoff**, appended to
`Docs/handoff-openbolt-2026-09-10-t564b-through-t575.md` on branch
`docs/handoff-openbolt-t559` (`79ce07fcd`). Not duplicated here.

### T584 piece 2 — the smoke went 4/7 and it was not the code

The three reds were about a backend the child was never connected to. The test
gated on "LM Studio is up and has a resident model", then launched a child that
read `<install>/settings.json` — `backend=llamacpp`, nothing on 7470. **Two
conditions were being conflated: the service is up, and the client is pointed at
it.** Fixed by telling the child (`LITETUI_BACKEND`); there is no `--backend` flag.

A second arm hid it: `"bracketed by turn_start and turn_end"` was GREEN on a turn
that produced nothing, because bracketing is satisfied by a turn that errored. It
now asserts `stopReason == "stop"` and prints the error text.

The file moved to `e2e/` — I had committed a live 27B smoke into the default suite,
which `tests/run_all.py` records as a thing already fixed once. Both gates now
apply: `testpaths` excludes `e2e/`, and `LITETUI_E2E=1` is required.

Re-runnable: `python e2e/consult_smoke_e2e.py` (8/8, needs LM Studio up with a
model resident — it skips by name otherwise and loads nothing).

### The through-line across all three cards

**An event is a claim, not evidence.** Every card this stretch was something
reporting a state nothing had produced:

- T584: `turn_end` arrived on a turn that had emitted nothing, and an arm called
  that a pass.
- T559: one assistant message per turn meant the timeline placed c4's answer where
  c2's first delta was stamped — the sort was right, the item's identity was not.
- T588: `batch-item-complete` after a pipeline that aborted, `model-switch` that
  loaded no model, `batch-complete` carrying a total equally true of ten successes
  and ten failures — and an API job marked `complete` from that event.

In every case the code read as correct and the unit was correct in isolation. What
was missing was asking, of each event, *what action does this assert, and did it
happen?*

### Two instrument lessons worth keeping

1. **`open(path, "w")` truncates before your payload exists.** A patch script threw
   on a bad escape mid-build and left a tracked 491-line test file at 0 bytes; only
   its being committed and clean made `git checkout --` a full recovery. The scripts
   now encode the whole payload before opening the target.
2. **`cmd | head` gives you `head`'s exit code.** I read `TSC_EXIT=0` off a `tsc`
   that had just emitted two TS2783 errors. Redirect to a file and read `$?`, or the
   gate reports on the pipe instead of the command.

Patterns recorded this stretch:

- `c30dbfa8…-1789094221` — a gate that checks the service is up but not that the
  client is pointed at it.
- `c30dbfa8…-1789094941` — a reported MISORDERING that is really a MERGE; if you
  cannot say where the item *should* sort, check whether it is a separate item.
- `c30dbfa8…-1789095635` — a try/catch around a generator that reports failure by
  RETURNING can never fire; the terminal event is the only discriminator.

### Open, and not mine

- **LiteTUI:** `--tool-profile scheduled` never reaches the turn. Installed at
  construction (`app.py:1209-1215`), then `_submit_text` overwrites it with
  `settings.tool_policy_profile` (`app.py:4272` → `:4296`) before `_stream()`. So an
  `--rpc` child runs tools as `autonomous` in the main checkout — the inverse of
  what ls-consult requires. The skill text says so; the code does not yet.
- **LiteImage:** `executePipeline` sets `this.cancelled = false` on entry, so each
  item resets the flag `executeBatch` checks at the top of its loop — a batch
  cannot be cancelled. One field, two owners.
- **LiteImage:** `inpaint` with no mask and `controlnet` with no control image still
  return the input UNCHANGED and report `step-complete`. Now that generation runs
  first they hand back a real image, so a batch reports success carrying a file that
  was never inpainted. Re-pinned in its new shape in `pipeline-batch.unit.test.ts`.
- **Still open from piece 1:** `C:/Projects/.claude/consult-config.json` does not
  exist; nothing in T584 creates it.

---

## The merge guard, and proof that its five files are instruments

Sentinel's merge guard (2026-09-10 23:0x) runs five cross-cutting files on every
LiteTUI merge, and seats run them before reporting a branch green:
`test_settings_controls.py`, `test_plugin_dogfood.py`, `test_tool_policy_wiring.py`,
`test_live_state_guard.py`, `test_headless_never_loads.py`.

It comes from SilverBolt's pattern `…-1789095945`: the settings gate existed, named
both fields, and the outage shipped anyway because nothing ran it against merged
main. **A gate outside the merge condition only dates the outage.**

To which the obvious follow-on is: a gate INSIDE the merge condition that cannot go
red only dates it more precisely. So each was mutated at its SUBJECT once, the arm
pre-registered before running. All six went red on the predicted arm; every restore
was byte-identical.

### Re-runnable record — mutate the SUBJECT, not the test

| # | guard | subject (the seam, not the test) | mutation | expect RED |
| --- | --- | --- | --- | --- |
| 1 | `test_settings_controls.py` | `src/litetui/settings.py`, the `Settings` dataclass | add a field with no control row (`0d099bc`'s shape) | `test_every_settings_field_has_a_control_or_is_exempt` |
| 2 | `test_plugin_dogfood.py` | `src/litetui/app.py` import block | add `from litetui.plugins.misc import _cmd_think` | `test_app_never_imports_a_plugin_module` |
| 3 | `test_tool_policy_wiring.py` | `src/litetui/app.py` `_fire_job`, the `profile =` line | take it from `job.tool_profile` | `test_the_SET_level_rides_with_queued_and_idle_cron_turns` |
| 4 | `test_live_state_guard.py` | `tests/conftest.py` `_never_write_the_live_task_store` | drop the three `monkeypatch.setattr` redirects | `test_saving_with_the_live_root_does_not_touch_the_live_store` (+2) |
| 5 | `test_headless_never_loads.py` | `src/litetui/app.py` `_ensure_chat_ready`, the `--rpc` refusal | delete the block | `test_NO_chat_request_leaves_an_rpc_child_for_an_unloaded_id` |
| 6 | `test_headless_never_loads.py` | `src/litetui/app.py` `_headless_model_decision` | `return ("ok", self.model_id, "")` first thing | 4 of 7, incl. `test_nothing_is_resident_so_it_REFUSES` |

6 is the unasked addition and the one worth keeping as a habit: 5 mutates the
ENFORCEMENT, 6 mutates the DECISION it enforces. **A guard that only watches the
call site cannot tell a correct decision from a rubber stamp.**

Readings at `main 495c4ce`; #4 re-run at `main 7b929b5` after the T592 follow-up
changed that exact fixture (`mkdir` moved onto the redirect branch) — still RED 3/6,
same three arms, restore `f459256f0eacb0933743fea2f51d8f8b`.

**Three of my first six anchors missed** (wrong dash count, wrong `profile =` line,
a `getattr` spelling). None was a guard failing — all were my text. A missed anchor
reports as "subject not found", which is the right shape: it cannot be mistaken for
a green.

### 🔴 "3/6" IS THE GUARD'S SENSITIVITY, NOT THE BLAST RADIUS

The #4 row above reads RED 3/6. That is three arms failing **in the one file I
ran**, and it would be a mistake to read it as what a conftest regression costs.
Sentinel's conftest rule exists because an autouse fixture touches every test, so
the row was measured against a question narrower than the rule's premise.

**Measured across fifteen — in fact twenty-five — files, at main `f2705cb`:**

> Gutting `_never_write_the_live_task_store`: **3 failed of 269 tests, and 1 file
> leaked — `background-tasks.json` in the repo root.** All three failures are in
> `test_live_state_guard.py`; baseline on the same set is 0 failed, 0 leaked.

The set is the 21 files the grep instrument in `51e2a89`'s body names on this tree
(`ast.parse|rglob|iterdir|os.walk|glob.glob`) plus the four merge-guard files that
grep does not already include. It is a SUPERSET of SilverBolt's ten rather than
his exact list, which is named in no artefact I could read — and picking a set by
guess produces a figure that looks authoritative while measuring something nobody
chose. A superset bounds the answer: every failure and every leak in it is real.

**THE RESULT IS THE OPPOSITE SHAPE TO THE ONE THE PHRASE SUGGESTS, AND IT
STRENGTHENS THE RULE.** The DAMAGE is suite-wide — any test touching the store
writes the live root. The DETECTION is concentrated in one file: 269 tests ran and
266 of them could not tell. That is precisely why a conftest edit needs a named
gate list rather than "the suite went green": nothing else goes red, so a
cross-cutting fixture regression is invisible to every instrument except the one
written for it.

Two incidental measurements from the same runs:

- The 25-file set **cannot run as one pytest invocation** — `INTERNALERROR
  SystemExit: 0`, the same shape `f2705cb`'s body reports for the 40. Two
  script-style files (`test_footer.py`, `test_ttyguard.py`) abort collection, so
  the figure above needed `run_all.py`'s own discriminator to split the runners.
  Script-style files never see conftest at all, so they sit outside this guard by
  construction.
- `test_kill_tree_honesty.py::test_a_real_tree_dies_by_HANDLE_CLOSE_and_the_grandchild_goes_with_it`
  failed **once** in a batch run and I nearly reported it as blast radius. It did
  not reproduce in a second mutated run and passed three baselines and three solo
  runs — a flake under batch load, not the mutation. Interleaving the arms rather
  than batching them is what separated the two.

### 🔴 PROVEN-INSTRUMENT IS A READING AT A SHA, NOT A PROPERTY

Every mutation in the table above is a measurement taken at a commit: the six at
`495c4ce`, #4 re-verified at `7b929b5`, the blast radius at `f2705cb`. Between the
first two of those, the T592 follow-up changed one of the fixtures being mutated —
inside the hour.

So the table is **re-run, never cited**. A row saying "this guard is an instrument"
is true of the tree it was measured on and says nothing about yours. If you need
the claim, spend the two minutes and take the reading again; if you find yourself
quoting a row instead, you are doing the thing the merge guard was written to stop.

### ⚠️ #4 did not just go red — it reproduced the outage

With the conftest redirects gone, the run wrote `background-tasks.json` into the
repo root: untracked, one row, `"label": "sleep 300"`, `"state": "running"`, id
`t-89b84f`. T592's own account is an untracked `background-tasks.json` carrying a
killed `t-343839 "sleep 300"`. Same file, same shape, different id. Deleted both
times; tree verified clean after.

Note where the guard lives: **`tasks.save` has no guard at all** — it writes
`Path(root) / STORE` unconditionally, which is correct for production. The
protection is the autouse fixture in `conftest.py`, so the subject of that guard is
test infrastructure. Do not go looking for a production seam that was never there.

### 🔴 #3 IS GREEN, IS A REAL INSTRUMENT, AND ITS NAME IS FALSE (carded T595)

`_fire_job` reads `profile = tool_policy.AUTONOMOUS` — a literal. Its own comment
says the conversation setting "deliberately does NOT reach here any more". The test
sets `settings.tool_policy_profile = AUTONOMOUS` and asserts `AUTONOMOUS`, so it is
agreeing with a constant.

Measured: changing that line to `tool_policy.unattended(self.settings.tool_policy_profile)`
— actually reading the setting, the thing the test's NAME claims — leaves it **5
passed, GREEN**. Both behaviours pass. The test cannot say which one is running.

Its docstring anticipated exactly this hazard and guarded the wrong pair:

> 🔴 THE SETTING IS AUTONOMOUS AND THE JOB IS SCHEDULED, DELIBERATELY. With both set
> to `scheduled` this test would pass whether the ruling was implemented or not.

That defends against the JOB being the source. A THIRD source appeared later — a
literal — and the precaution does not reach it, because the literal equals the value
the test chose. **Making two sources disagree proves nothing about a third.**

T595 is Ryan's ruling, not a seat's: AUTONOMOUS-always (rename the test, pin the
literal) vs ride-with-the-setting (change `_fire_job`, keep the name). Nobody edits
either until he picks.
