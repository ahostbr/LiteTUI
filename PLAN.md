# T070 — app.py decomposition: the agreed plan

**Status:** SilverBolt has written §0–§4 and §6 (the seal half, the joint contract, the reporting
rule). **§5 is OpenBolt's to complete and correct** — his steps are summarised there from
`PHASE1-OPENBOLT.md`, and a summary written by the other seat is not a commitment. He owns the
final wording of his own steps.

Inputs: `PHASE1-OPENBOLT.md` (12ab5a1) · `PHASE1-SILVERBOLT.md` (3ed952c). Neither seat has
touched `src/` on this branch.

---

## 0. THE CLOSE CONDITION, AND WHY THIS ROW IS BACK ON THE BOARD

The review already prescribed two extractions. **Both shipped.** The result:

| | before | after | |
|---|---:|---:|---|
| app.py lines | 5,271 | 5,204 | −1% |
| methods on `LiteTUI` | 125 | **137** | **went UP** |
| plugin reach-through | 35 | 35 | unchanged |

**Two successful extractions and the class grew back.** So the close condition is not "extract
until it feels smaller":

> 🔴 **DONE = all three metrics down together, and still down after the next feature lands.**

A step that moves one number is not progress unless the report says which one it moved *and which
it did not*. That is §6, and it is binding on every report in this task.

---

## 1. THE NUMBERS, EACH WITH THE INSTRUMENT THAT PRODUCED IT

**Ruling (Sentinel): the AST count replaces the grep count everywhere.**

| measurement | value | instrument |
|---|---:|---|
| plugin reach-throughs | **99** | AST over `plugins/**`, statements only (`plugin_reach.py`) |
| distinct members reached | **34** | same |
| plugin files reaching in | **13** | same |
| `_system` share | **49 of 99 (49%)** | same — every hit a call, zero reads |
| `LiteTUI` class-level defs | **137** | AST over `app.py` (`decomp_map.py`) |
| app.py total lines | **5,218** | `wc -l` |
| class lines / non-class lines | **4,256 / 739** | AST |
| methods in the shared-state knot | **35** (1,679 ln) | union-find over co-written attrs |
| methods writing no self state | **94** (1,666 ln) | same |
| suite | **collect on YOUR base**, never a remembered number | `pytest --collect-only -q` |

⚠️ **The grep count (102 / 35 / 14) is wrong and must not be quoted.** Its three extra hits are
**prose inside module docstrings** — `scheduler_ui.py:4`, `view_image.py:6`,
`skills_plugin.py:53` — files describing app internals in their own documentation.

🔴 **`_pending_tool_images` is a PHANTOM MEMBER.** It is the grep's 35th member and appears in
**no executable statement anywhere in `plugins/`**. It is not on the seal list. Anyone working
from the grep will hunt a caller that does not exist.

---

## 2. THE ORDER

Ruled by Sentinel. **S3 is second, not third** — it is the only step that moves all three metrics
at once, so it is the step that demonstrates the strategy works rather than one queued behind
cheaper wins.

| # | step | owner | moves | does NOT move |
|---|---|---|---|---|
| **S1** | `ctx.notify(text)`; `_system` keeps a 1-line delegation | SilverBolt | reach-through **99 → 50** | lines, methods, knot |
| **S3** | Relocate the eight owner-plugin methods into their plugins | **both — see §3** | **−321 lines, −8 methods, reach 50 → 42** | knot |
| **S2** | Point `convo.py` at `ConversationRepository` / `app.store` | SilverBolt | reach **42 → 34**; unblocks −6 aliases | lines, methods, knot |
| **S4** | Read-only `jobs` / `mcp_dispatch` properties | SilverBolt | reach **34 → 31** | everything else |
| **O0** | Lift 15 widget classes + 12 pure helpers to `litetui/widgets/`, `litetui/text/` | OpenBolt | **−739 lines** | **methods (137→137), reach (unchanged), knot** |
| **O2** | Clean-lift the six stateless groups (19 methods, ~244 ln) | OpenBolt | −244 lines, **−19 methods** | knot |
| **O3** | `_cron_*` → `CronService` | OpenBolt | −~130 lines, −4 methods | knot |
| **S5** | `ctx.model` facade (`model_switch.py`'s 20 hits) | SilverBolt | reach **31 → 11** | lines, methods, knot |
| **O4** | Seal the knot's plugin-facing members; split `_elapsed_*`/`_eta_*`/`_tps_*` off the knot | OpenBolt | −~200 ln, −~10 methods, **knot shrinks** | — |
| **S6** | `ctx.conversation` facade (`_compact` excluded) | SilverBolt | reach **11 → 5** | lines, methods, knot |
| **S7/O5** | The turn-engine boundary: `_stream` (345 ln), `_compact` (262), `_handle_command` | **both, last** | lines, knot | — |

**S1 first, unconditionally.** `_system` is 49 of 99, every hit a call, zero reads, return value
unused, four lines of body. Nothing else on either list is that cheap, and until it is sealed
every later extraction has to preserve a private name that 13 plugin files depend on.

---

## 3. 🔴 THE ONE PLACE WE WRITE THE SAME FILE — S3, AND THE ORDER IS NOT NEGOTIABLE

Eight methods move **out of `app.py` into the plugin that already owns the feature**. Each is
reached exactly once, by exactly that plugin:

| method | lines | owning plugin |
|---|---:|---|
| `_inbox_monitor` | 82 | `harness_plugin.py` |
| `_on_settings_saved` | 65 | `settings_ui.py` |
| `_cron_command` | 50 | `scheduler_plugin.py` |
| `_tool_view_image` | 44 | `view_image.py` |
| `_start_mark` | 31 | `mark_plugin.py` |
| `_mcp_server_names` | 22 | `settings_ui.py` |
| `_cron_monitor` | 16 | `scheduler_plugin.py` |
| `_register_custom_themes` | 11 | `themes_plugin.py` |

> **COMMIT 1 — SilverBolt: the body ARRIVES in the owning plugin. `app.py` untouched.**
> **COMMIT 2 — OpenBolt: `app.py` loses the method.**

**It cannot be the other way round.** The moment `app.py` loses `_inbox_monitor`, the plugin that
calls `app._inbox_monitor` breaks, and the suite is red *between* the two commits — which
destroys bisectability for everyone else on the branch. Arrival-first keeps every intermediate
commit green.

Per method or batched is SilverBolt's call. **The order is not.**

⚠️ `_inbox_monitor` (82 ln) is relocatable *and* knot-adjacent. If moving it whole drags shared
state into `harness_plugin.py`, it drops out of S3 and rejoins O4 — **decided by measurement at
the time, not now**, and the decision is reported either way.

---

## 4. EXPLICIT PREDECESSORS — dependencies as rows, not prose

A dependency mentioned once in a paragraph is a dependency someone executes out of order.

| this step | cannot start until | why |
|---|---|---|
| **O4** (seal the knot's plugin-facing members) | **S5 complete** | `_update_header`, `_fetch_ctx_window`, `_connect` are frozen by `model_switch.py` until the `ctx.model` facade replaces those 20 hits |
| **OpenBolt's alias cleanup** (delete the 6 `ConversationRepository` shims) | **S2 complete** | `plugins/convo.py` is the last caller keeping the shim alive |
| **S3 commit 2** (deletion) | **S3 commit 1** (arrival) | §3 — otherwise the suite is red between commits |
| **S7 / O5** (`_stream`, `_compact`) | **O4 complete** | both are inside the knot; boundary work before the knot splits is guesswork |
| everything | **T069 landed** | ✅ discharged — `9108042` + `b45863e`, pushed |

**S1, S2, S4 and O0 have no predecessors** and may run in any order or in parallel.

---

## 5. OPENBOLT'S HALF — HIS TO COMPLETE AND CORRECT

Summarised from `PHASE1-OPENBOLT.md` so the plan reads as one document. **These are his steps;
where this summary and his section disagree, his wins.** He should replace this section with the
detail he wants, including anything the summary above (O0–O4) states too loosely.

Carried forward from his analysis and not re-derived here:

- **96 connected components: one knot of 35 methods, and 95 singletons.** ~72% of methods write
  no instance state — the file reads as a monolith and mostly is not one.
- **34 members are framework-bound** (`compose`, `on_*`, `action_*`, `watch_*`, `@work` workers)
  and stay on the class by contract with Textual; the most they become is a one-line delegation.
- **`_stream` is 345 lines**, the largest method in the file, and is reached by a plugin.
- 🔴 **His O0 moves ONE metric and his own doc says so** — ~14% of the file, zero effect on method
  count or coupling. It is worth doing first for readability, and it is *not* decomposition
  progress on its own. That framing is exactly §6 and it originated with him.

---

## 6. REPORTING RULE FOR EVERY T070 COMMIT — RULED BINDING

> **Every report states which of the three metrics its step moved AND WHICH IT DID NOT.**

- **S1–S4 remove COUPLING.** They barely move the line count (only S3 does: −321 of 5,218 ≈ 6%)
  and they shrink the knot by **zero**.
- **O0 removes VOLUME.** It moves no method off the class and no reach-through.
- **Only O4 shrinks the KNOT**, which is the thing that made the two prior extractions fail to
  stick.

A step that removes coupling and not size is a success *reported honestly*. The same step reported
as "decomposition progress" is the exact failure that put this row back on the board.

---

## 7. FENCE — unchanged from the brief

- **Only OpenBolt writes `app.py`.** SilverBolt defines the API surface; OpenBolt implements the
  delegation inside the file. The single exception is S3, governed by §3.
- SilverBolt owns `plugins/**` and the new plugin API module.
- Neither seat rebases the shared worktree; Sentinel sequences any base move.
- `prompts/systemprompt.md` stays fenced and untouched.
- Gates per commit: full suite green against **your base's own collected count**, ruff no worse
  than base (`ruff check src` = 220 on `ab8b4a9`), diff scoped to the files the step names.
