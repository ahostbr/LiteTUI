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

📌 **A text gate counts the prose *about* the thing. Never size coupling from a grep.** Both seats
produced the AST number independently before it was adopted here.

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
| **O-A** | Delete the six `ConversationRepository` compatibility shims — **requires S2** | OpenBolt | **−6 methods**, −~10 lines | lines (barely), reach (already 0 via S2), knot |
| **S4** | Read-only `jobs` / `mcp_dispatch` properties | SilverBolt | reach **34 → 31** | everything else |
| **O0** | Lift 15 widget classes + 12 pure helpers to `litetui/widgets/`, `litetui/text/` | OpenBolt | **−739 lines** | **methods (137→137), reach (unchanged), knot** |
| **O2** | Clean-lift the six stateless groups (19 methods, ~244 ln) | OpenBolt | −244 lines, **−19 methods** | knot |
| **O3** | `_cron_*` → `CronService` — **scope reduced by S3, see §5** | OpenBolt | −~80 lines, −3 methods | knot |
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

> **COMMIT 1 — SilverBolt: the body ARRIVES in the owning plugin. `app.py` untouched. SUITE GREEN.**
> **COMMIT 2 — OpenBolt: `app.py` loses the method. SUITE GREEN.**

**Green at BOTH commits is the property, not a formality** — it is what keeps the branch bisectable
for everyone else while a two-seat move is half-done.

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
| everything that restructures `app.py` | **T069 MERGED INTO `main`** | 🔴 **NOT DISCHARGED — measured 2026-08-24.** `9108042`/`b45863e` are **pushed to `docs/adr-extraction`, which `git merge-base --is-ancestor origin/docs/adr-extraction origin/main` reports is NOT merged.** This branch does not contain them either. T069 rewrites a comment block at `app.py:1879` **inside `class LiteTUI`** (+5 −9). Restructuring `app.py` first is precisely what the brief says destroys that work silently. **PUSHED IS NOT LANDED.** |
| this branch | **a base move onto `main`** | its merge-base is `112e146`; `main` is `ab8b4a9`. The 2 missing commits are only `.github/workflows/ci.yml`, so nothing conflicts today — but **Sentinel sequences base moves, neither seat rebases the shared worktree.** |

**S1, S2, S4 and O0 have no predecessors** and may run in any order or in parallel.

---

## 5. OPENBOLT'S HALF — HIS TO COMPLETE AND CORRECT

**Completed by OpenBolt.** SilverBolt's summary above was accurate; this section supplies the
detail and makes **one correction to the §2 table**, which he invited.

### 🔴 CORRECTION TO O3 — S3 EATS MOST OF IT

The §2 row originally read *"−~130 lines, −4 methods"*, taken from my phase 1. **That figure is
pre-S3 and must not be planned against.** S3 relocates `_cron_command` (50 ln) and `_cron_monitor`
(16 ln) into `scheduler_plugin.py`, so by the time O3 runs the cron group is **~3 methods / ~80
lines**, not 5 / 148. Corrected in §2.
📌 The general form, and it applies to every row here: **an estimate made before its predecessors
is a measurement of a tree that will not exist.** Re-derive each step's numbers at the moment it
starts.

### THE STEPS

| step | what moves, concretely | expected effect |
|---|---|---|
| **O0** | 15 widget classes (`ThinkingBlock`, `CompactionCard`, `SkillAutocomplete`, `ToolMessage`, `ConfirmStop`, `AnswerBody`, `FoldBlock`, `PromptInput`, `CancelToolButton`, `ContextFooter`, `AssistantMessage`, `ThinkingHeader`, `_FoldHeader`, `ChatMessage`, `Completion`) → `litetui/widgets/`; 12 pure helpers (`tool_display_parts`, `_markdown_to_text`, `render_progress`, `thinking_header_text`, `tps_text`, `midturn_action`, `is_reliable_rate_sample`, `load_prompt`, `memory_prompt`, `_at_bottom`, `_mark_delivered`, `main`) → `litetui/text/` | **−739 lines.** Methods 137→137. Reach unchanged. Knot untouched. |
| **O-A** | delete the six `ConversationRepository` shims | **−6 methods.** Reach already 0 via S2. |
| **O2** | `_sync_*` (91 ln) · `_glassbox_*` (43) · `_load_*` (37) · `_append_*` (34) · `_store_*` (29) · `_convo_*` (10) → services | −~244 lines, **−19 methods.** Knot untouched. |
| **O3** | remaining `_cron_*` → `CronService`; the framework member delegates | −~80 lines, −3 methods |
| **O4** | `_elapsed_*` (4) · `_eta_*` (4) · `_tps_*` (3) off the knot behind one state object | −~200 ln, −~10 methods, **first step that shrinks the KNOT** |
| **O5** | `_stream` (345 ln) — with S7 | high risk, last |

### WHAT CANNOT MOVE, AND WHY

**34 framework-bound members** stay on the class by contract with Textual — `compose`, `on_*`
(message pump), `action_*` (bindings), `watch_*` (reactives), `get_system_commands`,
`check_action`, and `@work` workers. The most any of them becomes is a one-line delegation.

**Five members are frozen by plugins AND sit inside the knot:** `_update_header`,
`_fetch_ctx_window`, `_connect` (released by **S5**), `_stream` and `_resume` (not until **S7**).

### 🔴 O0 MOVES ONE METRIC AND THAT IS THE POINT OF SAYING SO

~14% of the file, and **zero** effect on method count, coupling, or the knot. Worth doing early
because it is nearly risk-free and makes everything after it readable — and **not decomposition
progress on its own.** Reported as such it would be the §0 failure repeated with more files.

### ⚠️ LOCAL GREEN ≠ CI GREEN RIGHT NOW, AND IT IS NOT OURS

`main`'s first real CI run (`32688014130`) is **red**: `2 failed, 1116 passed, 2 skipped`. Both are
environmental, not regressions:

- `test_seat_rebind.py::test_a_send_after_the_switch...` — `harness.py:35` puts `INBOX_ROOT` under
  `Path.home()/".liteharness"`, and `send()` at `:455` writes a temp file into `INBOX_ROOT.parent`
  **before any CLI call**. That directory does not exist on a clean runner, so the write raises and
  `send()` returns False. The test deliberately un-gates the harness (`:97`), which is what exposes
  it.
- `test_skills_visible.py::test_an_empty_directory_says_what_it_expects` — **mechanism not
  established.** `skills/` *is* tracked and *is* in the checkout, so "missing directory" is ruled
  out. Not guessing further.

⇒ **Judge T070 commits against LOCAL collection on your own base.** Do not read this pre-existing
red as a decomposition regression. Fixing those two is separate work and nobody owns it yet.

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
