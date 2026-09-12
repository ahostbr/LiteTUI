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

### Queued to me, in Sentinel's order

- **T689** — `background-tasks.json` loses rows between instances. Reproduction
  is written and lives in the T688 F+G commit body: A adds `task-A` and saves;
  B, loaded earlier, adds `task-B`; the file then holds `['task-B']`.
  ⚠️ **Do not fix it with a plain read-merge-write.** It is a LIST store, so a
  merge resurrects deleted rows from disk on the next save — a worse bug than the
  one being fixed, and one a green suite cannot see. It needs a deletion rule
  (tombstone, or a documented single-writer).
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
