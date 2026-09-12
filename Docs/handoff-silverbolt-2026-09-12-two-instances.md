# Handoff — SilverBolt, 2026-09-12, the two-instances run (T688 → T692)

Supersedes nothing. `Lands-at Docs/handoff-silverbolt-2026-09-12-two-instances.md`
on `feat/t692-docs`; there is no predecessor handoff for this run, so its absence
is not evidence of a lost file.

Seat: SilverBolt `1ccbc1d5-e16b-4022-b42e-8aa9659028c6`, worker.
Worktree: `C:/Projects/LiteTUI/.worktrees/silverbolt-t596`.
Everything below is LiteTUI unless it says otherwise.

## 1. In-flight

**One branch, pushed, not merged:** `feat/t692-docs` @ `b378b92` (README only).
Everything else in this run is on `main`.

```bash
git -C C:/Projects/LiteTUI log --oneline origin/main -6
git -C C:/Projects/LiteTUI merge-base --is-ancestor b378b92 origin/main   # false until merged
```

| card | my sha | merged as | what it was |
| --- | --- | --- | --- |
| T688 A–E | `d66cc81` | `18c2cdb` | `settings.save` read-merge-write + atomic |
| T688 F+G | `3a3be86` | `edfb608` | owner-exit takeover; eviction notice |
| T690 | `eafd6ee` + `fd52e9c` | `adfef3c` | second-instance VRAM modal, gate inside the backend |
| T691 | `d190c65` + `1b5a263` | `32635da` | per-conversation settings |
| T692 | `b378b92` | — | README |

Also merged earlier the same day, LiteSuite: T680 `ff8245b8d`, T681 `c5811baf3`,
T682 `9fcd6574e`, T683 (tip `93db1d213`).

## 2. Owed — split by owner

### Queued to me, in Sentinel's order — ALL LANDED, see §6

🔴 **THE T689 ROW BELOW WAS WRONG AND IS RETRACTED. IT IS KEPT SO THE
CORRECTION TRAVELS WITH IT.** I wrote that `background-tasks.json` was a list
store where a merge would resurrect deleted rows. The LAW is right; the FILE I
attached it to was not. Measured across the whole package including `plugins/`
and `rpc.py` — `grep -rn "bg_tasks" --include=*.py src/`, twelve hits — rows
are added at `app.py:1963` and mutated in place, and there is no `del`, no
`.pop`, no prune, no cap, no `/tasks clear` and no rpc delete. **Nothing in
this app ever removes a task row**, so the hazard did not exist for that file.
Where it DOES apply is `jobs.json`, which has three deletion paths
(`cron.py:70`, `goal_loop.py:406`, `rpc.py:361`). Carried in the Daily as
"handoff row on background-tasks.json retracted, law moved to jobs.json".

> **A LAW APPLIED WITHOUT CHECKING ITS ANTECEDENT IS A GUESS WEARING A RULE'S
> CLOTHES** — and in a handoff it is one the next reader has no reason to doubt.

~~- **T689** — `background-tasks.json` loses rows between instances.~~
~~Reproduction is in the T688 F+G commit body: A adds `task-A` and saves; B,~~
~~loaded earlier, adds `task-B`; the file then holds `['task-B']`.~~
~~⚠️ **Do not fix it with a plain read-merge-write.** It is a LIST store, so a~~
~~merge resurrects deleted rows from disk on the next save.~~
- **T694** — `tests/test_context_length.py::test_every_explicit_model_switch_applies_the_setting`
  counts switch sites in SOURCE TEXT and finds 2 where it wants 3. **Red on
  pristine `main`, not caused by anything in this run** — confirmed by running it
  in the main checkout at `edfb608`. Verify before touching:
  ```bash
  cd C:/Projects/LiteTUI && .venv/Scripts/python.exe -m pytest \
    "tests/test_context_length.py::test_every_explicit_model_switch_applies_the_setting" -q
  ```
- **T695** — `tool_policy_profile` write-through. Deliberately NOT done: 8
  assignment sites and several are TRANSIENT (a `goal_loop` override, two restore
  paths), so persisting all of them records a temporary elevation as the
  conversation's standing choice. It is carried in `ConvoSettings` and named in
  the exemption list of
  `test_EVERY_declared_field_has_a_writer_or_is_named_as_carried_only`.

### Sentinel's

- Merge `feat/t692-docs` `b378b92`.
- The **cancel-then-JIT residual** is with Ryan as a question: if the human
  cancels the VRAM modal, a later chat turn can still make LM Studio JIT-load
  server-side. No `backend.load()` is involved, so no gate can see it. Closing it
  means refusing the TURN rather than the load — a different decision from the
  one he made.

### Ryan's, and only his — none of it is verified

- **Two windows, same model** → no modal at all (the parallel design).
- **Two windows, different model** → the modal, every time, on `/model`, the
  picker, a `/modelcfg` ctx change, and rpc `set_model`.
- **Per-convo**: switch model/engine in A, open B and find it unmoved, `/resume`
  A and find it restored; `settings.json` untouched by either.
- **T688 A–E**: think level in one window survives a theme change in the other.
- **T688 F**: needs LiteSuite, not a second LiteTUI — start LiteSuite's router,
  attach LiteTUI, quit LiteSuite, send a message.

## 3. Absent by decision — and the gate that defends it

| Not done | The gate |
| --- | --- |
| Owner-pid field in `router.json` | Ryan ruled against it (a-62edbbe0): two instances SHARE a server. The measurement that prompted it still stands and is in pattern `1ccbc1d5-…-1789232668`, which SUPERSEDES `…-1789232136` precisely so the rejected recommendation cannot reach another agent as advice. |
| `tool_policy_profile` write-through | T695 above; the exemption is asserted in an arm, not just intended. |
| Merging `background-tasks.json` | T689 above; the deletion rule is the blocker, not the merge. |
| A Textual arm for the VRAM modal itself | `vram_dialog.py` has no arm; only the RULE behind it does (`second_instance.py`, every input combination). Drawing it needs a pilot, and the decision is what can be wrong. |

## 4. Caveats riding the pass lines

- **`test_context_length.py` is 1-failed/8-passed on BOTH sides.** Any run you do
  will show that red. It is T694, not you.
- **ruff has a large inherited baseline.** Compare, never read the total:
  `app.py`+`llm_backend.py` = 80 before / 81 after my last change, and the one
  added is `S110` on `_record_load_settings`, matching 3 the same file carries.
  Always diff against a pristine copy from `origin/main` **in a real checkout** —
  running ruff on copies outside the repo answered differently for me once.
- **`tests/test_settings_live.py` is not a pytest module.** It is a script whose
  `main()` calls `sys.exit`, and including it aborts collection with
  INTERNALERROR. Exclude it by name.
- 🔴 **`conftest.py` now isolates two live things, and both were real bugs.**
  `AGENTS_DIR` (T690 — the suite otherwise passed or failed depending on whether
  Ryan had a LiteTUI window open) and `_DEFAULT_VRAM_GATE`/`_DEFAULT_LOAD_HOOK`
  (a dead App's bound method answering for a live one). Do not "simplify" either.
- ⚠️ **`router_record.record_path()` is `~/.litesuite/llm/router.json`** — NOT
  under the data root, shared with LiteSuite. Redirecting `LITETUI_DATA_ROOT` is
  **not** isolation for it; patch the function.
- **UNPROVEN, carried forward:** no arm drives the real modal, and no arm drives
  two real processes. Every "two instances" arm simulates the second one.

## 5. My own corrections and retractions

1. **I proposed an owner-pid field and Ryan ruled the opposite.** My analysis of
   the mechanism was sound and my inference about the GOAL was invented: I read a
   collision as a thing to prevent when the owner wanted it allowed. Measure what
   IS happening (knowable); take whether it should be prevented, serialised or
   made concurrent to the human as a QUESTION with options, never as a
   recommendation with one.
2. **T690 first cut gated ONE call site.** Root cause: my grep was
   `src/litetui/*.py`, which does not include `plugins/` — where the most common
   swap in the app lives. The fix moved the gate into the backend.
3. **T691 first cut declared four fields and wired none of them** — and my own
   arm agreed, because it asserted the FILE round-trips them. It does. A schema
   is a promise about shape, not behaviour. The replacement arm is derived from
   `fields(ConvoSettings)`.
4. **A detector of mine was blind to its own subject.** The disk-derived
   "every load entry point is guarded" arm stayed GREEN under the mutation that
   turned two sibling arms red, because it accepted "delegates to something
   guarded" while the mutated function also had an unguarded branch. An escape
   hatch not scoped to the whole body is an escape hatch for the whole body.
5. **I staged a commit by naming four files and orphaned an arm.** The T688
   amendment arm never left the worktree and only `git status` would have said
   so. After a named-file stage, read the porcelain.

## Suggested skills

- None to continue any of this. `/ls-conversation-lookup` before claiming
  anything in this workspace does not exist.


## 6. What landed after this doc was written (T689 → T703)

Every row names a sha on `main`. All UNVERIFIED by Ryan.

| card | merged as | what it was |
| --- | --- | --- |
| T689 | `dc0be0a` | two windows no longer erase each other's task and job rows; a sibling's live task is not reported LOST; `jobs.json` written where it is read |
| T694 | `6623608` | the switch-site count in `test_context_length` was pinned to a source LAYOUT, not the rule |
| T695 | `aba39c9` | the tool authority a conversation was SET to is per-conversation; every transient elevation stays transient |
| T699 | `1130286` | two files named `test_*` were scripts that killed the whole run |
| T698 | `ccc9251` | the model picker goes through the one switch path |
| T700 | `881ed0d` | ten more such scripts; an AST scan stops the class recurring |
| T702 | `39a47c4` | ONE module-level-exit rule shared by the runner and the detector; thirteen misfiled files move |
| T703 | `7196775` | the cancel-then-JIT READING (`Docs/Plans/litetui-cancel-then-jit.md`), no code |

### The three things a successor most needs

1. 🔴 **`row_store` takes a BASELINE, and where it is taken decides two silent
   failures.** `src/litetui/row_store.py`. Taken after a load-time mutation, the
   mutation is not in the delta and never reaches disk; taken as everything on
   disk, a row the loader could not parse looks DELETED and the next save erases
   it. Both loaders therefore baseline the rows they KEPT, before changing their
   mind about any of them.
2. 🔴 **A delta reconciles two PROCESSES and cannot reconcile two holders inside
   one.** That is why `rpc._handle_jobs` now edits `app.jobs` instead of loading
   its own copy. If a second in-process holder ever appears, no merge rule fixes
   it.
3. ⚠️ **`_active_tool_profile` has nine writers and exactly one is a CHOICE.**
   `set_tool_profile` only. Mail, a cron fire, a goal loop and the queued flush
   are all transient; persisting any of them records a temporary elevation as
   the conversation's standing authority. `chosen_tool_profile` is the source
   the two non-transient reads consult.

### Open on Ryan alone

- T689: two windows, long task in A → stays `running` in both; quit A → LOST.
- T689: `/cron rm` in one window, edit another job in the other, third boot.
- T689: `LITETUI_DATA_ROOT` set → a schedule's `last_fired_slot` survives a
  restart and no `jobs.json` appears in the checkout.
- T695: set the authority in one conversation, find another unmoved, `/resume`
  the first — then let a cron job fire and confirm the authority did NOT follow.
- **T701** — `tests/test_footer_fields.py` is 6-failed/4-passed on pristine
  `main`; the footer renders its compact form where the arms expect `think:`,
  `ctx 60,000 / 120,000` and tok/s. `tests/test_footer.py` passes 34/34 on the
  same surface. Two footer suites disagreeing, and since T699 they disagree in
  the same run. Not mine; queued for his eye.
- **T703** — refuse-and-stay / refuse-and-roll-back / refuse-once-then-allow.

### Traps that cost me time in this run

- **A branch with no commit is not a save point.** `git checkout <branch> --
  <files>` reads that branch's TREE, and an uncommitted branch's tree is its
  base — it destroyed ~90 lines of new arms. Recovered only because the edits
  came from scratchpad generator scripts. Pattern `1ccbc1d5-…-1789238583`.
- **A linter must be run INSIDE the project.** Comparing ruff on copies outside
  the repo answered 16 → 20 where the truth was 19 → 17: wrong count and wrong
  DIRECTION, because `pyproject.toml` does not follow the files. Pattern
  `1ccbc1d5-…-1789240395`.
- **A source file under a test run is live state, not a document.** `ruff --fix`
  during a gate that parses those files produced a red that was mine, not the
  code's.
- **`tests/run_all.py` is the repo runner and `classify()` decides what it
  executes.** Since T702 the script half is EMPTY; if an arm ever ranges over it
  again it is measuring nothing.

### Suggested skills

- None. `/ls-conversation-lookup` before claiming anything here does not exist.
