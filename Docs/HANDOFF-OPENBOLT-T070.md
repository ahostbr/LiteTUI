# HANDOFF — OpenBolt, T070 (app.py decomposition)

**Supersedes** nothing; this is my first T070 handoff. Written before starting O-A, at Ryan's
instruction, ahead of a compaction.

🔴 **EVERY ROW BELOW IS A SYMBOL, A QUERY OR A SHA. Re-run the query; do not trust the cell.**

---

## 1. WHERE THINGS STAND

| fact | query |
|---|---|
| branch tip | `git rev-parse --short origin/refactor/app-decomposition` → **`22a7834`** |
| my last commit | **`22a7834`** — O2, pushed |
| `LiteTUI` method count | `git show HEAD:src/litetui/app.py \| grep -cE '^    (async )?def '` → **131** |
| `app.py` lines | `git show HEAD:src/litetui/app.py \| grep -c ''` → **4,450** |
| gate | `uv run --locked python tests/run_all.py` → **EXIT 0, 1115 passed** |
| in flight from me | **NOTHING.** `git status --porcelain` — anything dirty is another seat |

**Trajectory, all at refs:** `app.py` 5,242 → 4,450 (−792) · methods 139 → 131 · the 35-method
shared-state knot **untouched by every commit so far**.

---

## 2. WHAT I DID, AND WHAT EACH STEP DID *NOT* MOVE

| sha | step | moved | did NOT move |
|---|---|---|---|
| `d7e13d0` | S1 arrival: `system_message` + `_system` alias | nothing (+28 lines) | all three metrics |
| `e6b7ee9` | O0 b1: 10 pure helpers → `textfmt.py` | −106 lines | methods, reach, knot |
| `4e6125e` | O0 b2: 18 widgets → `widgets.py` | −586 lines | methods, reach, knot |
| `91987dd` | notation fix `_x`/`_x_*` | nothing | everything |
| `22a7834` | O2: 8 stateless methods → `appsvc.py` | **−8 methods**, −128 lines | reach, knot |

**O0 and O2 both RE-EXPORT what they move** (`app.py` imports the names back), because tests reach
them as `from litetui.app import ToolMessage` — 16 outside references to that one class. Without the
re-export these are breaks, not moves. **They shorten a file; they do not shrink an API.**

---

## 3. OWED — SPLIT BY OWNER

### 3a. MINE, NEXT
- **O-A** — delete the six `ConversationRepository` shims. **NOT zero-edit:** 13 test call sites
  across 5 files, same commit. `_list_convos` is CLEARED (its "product caller" was a docstring).
- **O3** — `_cron`/`_cron_*` → `CronService`. **Scope shrinks if S3 lands first** (S3 relocates
  `_cron_command` 50 ln and `_cron_monitor` 16 ln).
- **S5 ARRIVAL** — queued by SilverBolt (msg `9bf9fae2`): five public names + five aliases,
  **three are `@work` decorated** — copy the decorator with the def. Strictly arrival-first; all
  five names verified 0 at HEAD.
- **The two address-coupled methods** — `_sync_seat_identity`, `_sync_fleet_identity`. Their tests
  assert them BY NAME against `app.py` source. Needs a considered commit that re-scopes those
  gates, not a batch move.

### 3b. THEIRS
- **SilverBolt** — S5 consumer (18 edits) after my arrival; S3 arrivals; `skills_plugin.py:57`
  docstring naming `_append_to_system`, which now lives in `appsvc`.
- **Sentinel** — merge to `main`. Branch is **not** merged.

### 3c. RYAN'S
- **Main's CI is red** and it is NOT ours: 2 environmental failures
  (`test_seat_rebind`, `test_skills_visible`). Nobody owns the fix; it needs its own task.

---

## 4. ABSENT BY DECISION — AND THE GATE THAT DEFENDS EACH

| not done | what stops it coming back |
|---|---|
| `_append`, `_load_system_prompt` not lifted | plugin callers ⇒ seal work, and I must not edit `plugins/` |
| the 4 `_convo_pending`/`_convo_loading` accessors not lifted | a `@property`/`@setter` pair is the class's attribute interface; lifting one is a rename with extra steps |
| `harness.py:294` docstring NOT edited | it narrates `_sync_seat_identity`, which I **dropped** from the batch — it still lives in `app.py`. Editing it would introduce the error |
| no rebase of the shared branch | merge is allowed and needs no permission; **rebase never is** |

---

## 5. CAVEATS RIDING THE GREEN

- ⚠️ **`run_all` EXIT CODE is the gate — never the pytest count. PROVEN ON MY OWN WORK, NOT
  ARGUED.** A run of O2 read **`1115 passed`** and **`RUN_ALL EXIT=1`**: the pytest half was
  ENTIRELY GREEN while two SCRIPT-half tests (`test_footer.py`, `test_view_image.py`) were broken.
  **Quoting the pytest number would have reported a green measuring 93 of 106 test files.**
  The split is `pytest-style: 93 · script-style: 13`, and `pytest` cannot see the second group.
- ⚠️ **No metric from the working tree.** Three seats share one checkout, so `git status` reports
  the union and attributes none of it. `git archive <ref> | tar -x` then measure.
- ⚠️ **Re-validate the verify tree IMMEDIATELY BEFORE the run you quote**, not when you build it —
  it goes stale mid-use when another seat commits.

---

## 6. MY OWN CORRECTIONS

- **I made the `_x_*` prefix bug independently of Sentinel and reported the PLAN as stale on the
  strength of it.** Retracted; the plan was right. Two seats, one notation, identical defect —
  **when two seats agree on a surprising number that is not corroboration**, because the inputs
  were not independent.
- 🔴 **THE FOUR-DEFECT TALLY — the argument for the gate, stated as measured:**

  > **Four mechanical transforms, every one producing something that LOOKED finished. Three of the
  > four were invisible to `py_compile` AND `pyflakes`. And the second-to-last would have shipped a
  > feature that was STRUCTURALLY PRESENT AND BEHAVIOURALLY DEAD.**

  | # | defect | how it presented |
  |---|---|---|
  | 1 | carried imports but **not module constants** | `STORE_HEADER`, `GLASSBOX_MIN_INTERVAL_S`, `MAX_IMAGE_DIM` undefined — a compile step never resolves a global |
  | 2 | address detector matched `'NAME'`, missed `"NAME()"` | cleared a method that two tests pin BY NAME |
  | 3 | **sibling rewrite DELETED the receiver instead of moving it** | `glassbox(channel, 1.0, …)` shifted every arg left; `getattr(1.0,"plugins",None)` → None → **early return, channels silently stopped firing.** Compiled, ran, emitted nothing |
  | 4 | import inserted **below** the class using it | `NameError` at class-definition time, script half only |

  Only running the thing found 1, 3 and 4.
- **CORRECTED IMPORT RULE, so nobody repeats defect 4:** insert at **the last import BEFORE the
  first `class` or `def`** — *never* the last import-looking line in the file. Two of these tests
  carry late top-level imports below their fake-app class.
- **My "stateless" classifier was too narrow** — `app._gb_last[ch] = now` is a SUBSCRIPT store,
  which it read as a load. A read of a mutable object is a write channel.
- **My grep counted docstrings as call sites** three times, once fencing `_list_convos` for a
  caller that was prose. Then my *fix* for that over-reported, flagging `_glassbox` on two
  docstring hits.

⇒ The line that covers all of it: **an instrument that answers a narrower question than the one you
asked returns something SHAPED LIKE an answer.**

---

## 7. SUGGESTED FOR THE NEXT SESSION

`/arch` · `/library` · `/liteharness` at start. Then read `PLAN.md` §8 (measurement protocol) and
§9 (notation) **before** measuring anything, and `BRIEF.md` §2 for why "extract until it feels
smaller" already failed here once.
