# T070 Phase 1 — OpenBolt (the extraction half)

Read-only analysis. No file in `src/` was modified. All numbers derived by AST parse of
`src/litetui/app.py` at `112e146`, scripts in this session's scratchpad
(`decomp_map.py`, `decomp_groups.py`, `decomp_frozen.py`) — re-runnable, not eyeballed.

---

## 0. THREE CORRECTIONS TO THE BRIEF, ALL VERIFIED AGAINST THE CODE

**a. "193 definitions" is a FILE-WIDE count, not the god class.** `LiteTUI(App)` has **137**
class-level defs — which is exactly the `137` the review's own regression table reports, so the
two numbers in the brief are measuring different things. The other **56** defs live in 15 other
classes and 12 module-level functions that merely *share the file*.

**b. app.py is not all god class.** The class is **4,256 lines**; **739 lines are not it** —
widgets (`ThinkingBlock`, `CompactionCard`, `SkillAutocomplete`, `ToolMessage`, `ConfirmStop`,
`AnswerBody`, `FoldBlock`, `PromptInput`, `CancelToolButton`, `ContextFooter`, …) and pure text
helpers (`tool_display_parts`, `_markdown_to_text`, `render_progress`, `thinking_header_text`,
`tps_text`, `midturn_action`, `is_reliable_rate_sample`, `load_prompt`, `memory_prompt`).

**c. §5 contradicts §1.** §1 corrects the target to **1,120 / 0**; §5 still says "green at
**1,134**/0". §1 is right for base `main`. Also the base moved: `main` is now **`ab8b4a9`**, not
`112e146`.

---

## 1. THE COUPLING IS ONE KNOT, AND IT IS SMALLER THAN THE FILE SUGGESTS

Separability rule: shared **reads** are a dependency and can be injected; shared **writes** are
shared mutable state and are what actually blocks a carve. Union-find over co-written attributes:

```
methods on the class                 130 parsed (137 defs incl. properties/overloads)
in the ONE shared-state knot          35   (1,679 lines)   <- the real problem
write NO self state at all            94   (1,666 lines)   <- separable TODAY
framework-bound (must stay)           34
distinct self attrs written           56
```

**96 connected components: one knot of 35, and 95 singletons.** The file reads as a monolith and
is mostly not one. ~72% of the methods write no instance state at all.

**Hub state** — the attrs the knot is built around: `_pending_input`, `_jobs`, `_inflight_tools`,
`_convo_pending`, `_follow_anchor`, `_eta_*`, `_elapsed_*`, `conversation`, `client`, `model_id`.

---

## 2. CANDIDATE GROUPS

| group | defs | lines | in knot | framework | verdict |
|---|---:|---:|---:|---:|---|
| `_cron_*` | 5 | 148 | 0 | 1 | MIXED — logic lifts, the framework member delegates |
| `on_*` | 7 | 123 | 3 | 7 | STAYS — Textual message pump |
| `action_*` | 8 | 106 | 4 | 8 | STAYS — Textual bindings |
| `_sync_*` | 3 | 91 | 0 | 0 | **CLEAN LIFT** |
| `_glassbox_*` | 3 | 43 | 0 | 0 | **CLEAN LIFT** |
| `_load_*` | 3 | 37 | 0 | 0 | **CLEAN LIFT** |
| `_append_*` | 3 | 34 | 0 | 0 | **CLEAN LIFT** |
| `_store_*` | 2 | 29 | 0 | 0 | **CLEAN LIFT** |
| `_convo_*` | 5 | 10 | 0 | 0 | **CLEAN LIFT** |
| `_tool_* / _deliver_* / _elapsed_* / _eta_* / _tps_* / _refresh_*` | 18 | 295 | 12 | 0 | BLOCKED — inside the knot |

---

## 3. WHAT CANNOT MOVE

34 framework-bound members: `compose`, `on_mount`, every `on_*` (message pump), every `action_*`
(bindings), `watch_*` (reactives), `get_system_commands`, `check_action`, and `@work`-decorated
workers. These stay on the class **by contract with Textual** — the most they can become is a
one-line delegation.

**Plus five members frozen by plugins** (see §4): `_update_header`, `_fetch_ctx_window`,
`_connect`, `_stream`, `_resume` are all *inside the knot* and reachable from `plugins/`, so they
cannot be moved or renamed until the seal lands. `_stream` alone is **345 lines** — the largest
method in the file.

---

## 4. 🔴 THE INPUT SILVERBOLT NEEDS, AND IT REFRAMES HIS HALF

**102 reach-throughs are only 35 distinct members, and ONE accounts for half of them.**

```
50 of 102  (49%)   _system          <- 4 lines, writes NOTHING, not in the knot, not framework-bound
 6         _update_header    6  _fetch_ctx_window    5  _connect    3  _jobs
top five = 70 of 102 (69%)          14 plugin files reach in
```

⇒ **Half the coupling dissolves behind one supported `notify()`/`emit()` API**, because `_system`
is the easiest possible member to seal: stateless, 4 lines, no framework binding. It is not 102
problems; it is one problem plus a 34-member tail, and the tail's top four are all in the knot.

---

## 5. PROPOSED ORDER — SMALLEST RISK FIRST, WITH HONEST EXPECTED EFFECT

The brief's warning is that two prescribed extractions shipped and the class **grew back**. So each
step below states which of the three metrics it moves and, importantly, **which it does not**.

| # | step | lines | methods | reach-through | risk |
|---|---|---|---|---|---|
| 0 | Lift the 15 widget classes + 12 pure helpers out of `app.py` (739 lines) into `litetui/widgets/` and `litetui/text/` | **5,218 → ~4,480** | 137 → 137 | 102 → 102 | very low — no class change, no shared state |
| 1 | **SilverBolt:** supported API for `_system`; app.py keeps a 1-line delegation | ~0 | 137 → 137 | **102 → ~52** | low |
| 2 | Clean-lift the six stateless groups (§2, ~244 lines / 19 methods) into services | ~4,480 → ~4,240 | **137 → ~118** | — | low |
| 3 | `_cron_*` → `CronService`; framework member delegates | −~130 | −4 | — | low |
| 4 | Seal the knot's four plugin-facing members, then split `_elapsed_*`/`_eta_*`/`_tps_*` off the knot behind a small state object | −~200 | −~10 | ~52 → small defended set | medium |
| 5 | `_stream` (345 lines) — last, and only behind the suite | −~200 | — | — | high |

🔴 **Step 0 moves ONE metric and I am saying so.** It cuts the file by ~14% and does nothing to
method count or coupling. It is worth doing first because it is nearly risk-free and it makes
everything after it easier to read — **but if it were reported as "decomposition progress" it would
be the exact failure §2 describes.** Progress is all three numbers moving down together.

**Order rationale:** step 1 before steps 2–4, because until `_system` is sealed every extraction has
to preserve a private name that 14 plugin files depend on. Sealing 49% of the coupling first makes
the later moves cheaper.

---

## 6. OPEN QUESTIONS FOR SENTINEL

1. **The worktree is shared with SilverBolt.** `main` moved to `ab8b4a9` and this branch is on
   `112e146`. I will not rebase a tree another seat may be working in — **say when, and who.**
2. **T069 must land first** (it moves 4 dated comments in `app.py`). Name the sha; until then I
   treat `app.py` as not mine to restructure, per the brief.
3. **Confirm the target count** for this base once it is rebased: `uv run --locked pytest
   --collect-only -q`. On `112e146` it is 1,120.
