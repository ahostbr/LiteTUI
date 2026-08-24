# HANDOFF — OpenBolt, T070, 2026-08-24

**Supersedes `HANDOFF-OPENBOLT-T070.md`** (verified present: `git ls-files --error-unmatch
HANDOFF-OPENBOLT-T070.md`). Written at Sentinel's order ahead of a compaction.

🔴 **EVERY ROW IS A SYMBOL, A QUERY OR A SHA. Re-run the query; do not trust the cell.**

---

## 1. O4-a — LANDED AND GREEN

| fact | query |
|---|---|
| sha | **`15f7668`** `refactor(app): O4-a — the _eta family becomes turnstats.EtaState` |
| pushed | `git ls-remote origin refs/heads/refactor/app-decomposition` → matches local |
| new module | `src/litetui/turnstats.py`, **in the commit** — it is no longer untracked |
| gate | `uv run --locked python tests/run_all.py` → **EXIT 0**, 93 pytest files + 13 scripts |
| in flight from me | **NOTHING.** `git status --porcelain` → 0 |

**Trajectory, all at refs:** `app.py` **4,333 → 4,297**; methods **125 → 121**; knot
comps>1 **43 → 41**; largest component **13 → 13**.

## 2. 🔴 THE PRE-REGISTERED NUMBER — MEASURED, AND THE HONEST READ IS NOT "PREDICTION HIT"

Predicted for `_eta`: **6 INERT sites.** Measured: **all six were in `app.py`**, the file that was
moving, so they left *with* the methods and the sweep outside `app.py` was **ZERO**.

⇒ The prediction was correct and **self-resolving**. `1 file(s)` in the pre-cost was doing more work
than `6 inert`, and **I should have read the file count as the risk signal.** For `_tps` the two
diverge — **19 inert across 4 files** — and that is where the number is actually tested.

**The knot prediction stands unfalsified so far:** pre-registered largest component **13 → 13
unchanged** for all of O4, with the real effect in **comps>1 43 → 34**. O4-a delivered **43 → 41**.

## 3. OWED — SPLIT BY OWNER

### 3a. MINE
- **O4-b `_elapsed`** — 13 inert, 1 file. `python tools/move_cost.py _elapsed_cancel _elapsed_start
  _elapsed_stop_body _elapsed_repaint --ref HEAD`
- **O4-c `_tps`** — 19 inert, **4 files**, and `tps` is **PUBLIC** (8 `src/` read sites). The
  `_on_settings_saved` shape: a field the private-reach metric cannot see. **Last, deliberately.**
- Target module exists: `src/litetui/turnstats.py`. Add `ElapsedState` / `TpsState` beside `EtaState`.

### 3b. SILVERBOLT'S
- **S6 consumer** — his own numbers are **WITHDRAWN by Sentinel** until re-derived **src-wide**
  with the three shapes + reads/writes split. `tools/move_cost.py` is the instrument.
- **S3 `_tool_view_image`** — ruled to **stay in `app.py`**. Not his, not mine, not O4's.

### 3c. RYAN'S
- **main's CI is red** from 2 environmental failures (`test_seat_rebind`, `test_skills_visible`).
  Unowned, needs its own task. Unchanged since the last handoff.

## 4. ABSENT BY DECISION — EACH WITH THE GATE THAT DEFENDS IT

| not done | what stops it coming back |
|---|---|
| `_fire_job` not in O3 | writes `_pending_input` + `_active_tool_profile`, drives `_stream`. Turn-engine seam. **With it held, every O3 member writes only `jobs`** |
| `_elapsed_repaint` stays on the app | reads `_elapsed_body_t0`, `tps`, and calls `_eta` ×2 — it is a **VIEW**. Moving it makes one object reach into two others |
| `_tool_view_image` stays | `_pending_tool_images.append(...)`; `view_image.py:6` calls it "AGENT-LOOP INFRASTRUCTURE"; **reach 1 → 1, zero metric gain** |
| `_sync_seat_identity` / `_sync_fleet_identity` withdrawn | §5c: mutating methods are never stateless lifts |
| `glassbox` **not** re-lifted | §3h: **OPEN, on ice.** `_gb_last` has 1 production reader and 1 writer, both itself; a service object for one dict is single-use abstraction |
| largest-component metric **kept**, not swapped | reported **alongside** comps>1, with the blindness stated. Switching metrics silently is the failure |

## 5. CAVEATS RIDING THE PASS LINES

- ⚠️ **`run_all` EXIT CODE is the gate, never the pytest count.** 93 pytest + 13 script files;
  `pytest` cannot see the second group.
- ⚠️ **`tests/test_cron_wiring.py` CANNOT PASS ALONE AT ANY REF** — 15 failed at `c8bff39` too.
  The first `LiteTUI()` under a monkeypatched `paths.ROOT` fails importing `core_tools`.
  `pytest tests/test_first_boot.py tests/test_cron_wiring.py` → 22 passed. **It invalidated two
  rounds of my gate proofs and both times the CONTROL ARM said so.**
- ⚠️ **My reach figure is 17 with a scope**: AST call sites on LiteTUI's private surface, receiver
  pinned to `app`, over `src/litetui/plugins/**` only. Sentinel's instrument reads 22. **Different
  denominators — do not quote a bare 17.**
- ⚠️ `move_cost.py` **groups by receiver and does not guess.** Read the grouping before believing a
  total; `--receiver app` filters after you have looked.

## 6. MY OWN CORRECTIONS AND RETRACTIONS THIS STRETCH

- 🔴 **O2's headline went −8 → −4.** Half of what `22a7834` claimed is withdrawn: four functions
  returned from `appsvc`. **I found the classifier error, wrote it in the commit body, and shipped
  the lift it had wrongly cleared in the same commit.** *Documenting a flaw is not fixing it.*
- 🔴 **I costed glassbox's return at 1 method; it was 3.** Two one-line wrappers ride on it.
- 🔴 **O3's pre-cost was 9 sites in 3 files; the real sweep was 55 in 18.** I costed the METHODS and
  never the STATE the service exists to own — and my widened census scanned `tests/` only, missing
  6 product sites in `goal_loop.py`.
- 🔴 **I walked into the inert-stub hazard I had advised SilverBolt about**, rewriting
  `self._system` → `app.system_message` in `cron.py`. 13 tests broke; **the deviation bought nothing**
  because `cron.py` is not the plugin tier.
- 🔴 **`move_cost` skipped `app.py` silently in its first hour** — a skip branch with no counter,
  inside the tool built to catch exactly that. It was hiding **14 real sites**.
- 🔴 **I documented an exit-code collision instead of removing it.** Fixed at `e1b8485`: exit **3**
  is "nothing was examined", **2** belongs to argparse. A typo must never read as a finding.

## 7. TOOL STATUS — ALL COMMITTED, NONE LOCAL

| tool | sha | state |
|---|---|---|
| `tools/move_cost.py` | `df82ef8` → `e1b8485` | **committed**, incl. the skip fix, the def-shape fold-in, and the exit-3 split |
| `tools/comment_census.py` | SilverBolt's `e9f30bd`/`c61343d` | **committed**; `python tools/comment_census.py` → TOTAL 0, REAL EXIT 0 |

**Run `move_cost` with the MEMBERS FIRST** — `--roots` is `nargs="*"` and greedy.

## 8. SUGGESTED FOR THE NEXT SESSION

`/arch` · `/library` · `/liteharness`. Then **PLAN §3a–§3k** (the S3/O3 re-derivations, the write
detector's four sightings, the isolation artifact) and **§2f** (S6's four def-splitting gates) before
measuring anything. `BRIEF.md` §2 for why "extract until it feels smaller" already failed here once.
