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
| **S1** | **`app.system_message(text)` — a PUBLIC method on the app**, `_system` kept as a one-line alias; SilverBolt rewrites the 49 call sites | **both — see §2a** | private reach-through **99 → 50** | lines, methods, knot |
| **S3** | Relocate the owner-plugin methods into their plugins — **NOW THREE, not eight** (`_mcp_server_names` · `_tool_view_image` · `_start_mark`). Cron → O3 (Ryan); `_inbox_monitor` + `_on_settings_saved` + `_register_custom_themes` → §3b/§3c | **both — see §3** | **−97 lines · −3 by ALL THREE units** (defs = class-body items = names; none is an alias) **· reach NET −1** (removed 3, added 2, AST call SITES) **· 0 invisible plugin→plugin edges · 0 app→plugin edges** | knot |
| **S2** | Point `convo.py` at `ConversationRepository` / `app.store` | SilverBolt | reach **42 → 34**; unblocks −6 aliases | lines, methods, knot |
| **O-A** | ✅ **DONE `b1dd935`.** Delete the six `ConversationRepository` shims — **requires S2**. **NOT zero-edit: 2 are free, the other 4 need 13 test sites across 5 files fixed IN THE SAME COMMIT** or the deletion is red — see §2c | OpenBolt | **6 names / −4 `def`s / −7 class-body items**, −19 lines — **see the unit note in §2c** | lines (barely), reach (already 0 via S2), knot |
| **S4** | `jobs` / `mcp_dispatch` properties — **`jobs` is NOT read-only, see §5a** — **ARRIVAL-FIRST on OpenBolt's two properties** | SilverBolt + OpenBolt | reach **43 → 40** (AST) | everything else |
| **O0** | Lift 15 widget classes + 12 pure helpers to `litetui/widgets/`, `litetui/text/` | OpenBolt | **−739 lines** | **methods (137→137), reach (unchanged), knot** |
| **O2** | Clean-lift the six stateless groups — **12 methods / 212 ln, see §5b** | OpenBolt | −212 lines, **−12 methods** | knot |
| **O3** | `_cron`/`_cron_*` → `CronService` — **scope ENLARGED, not reduced: S3 no longer touches cron at all. `_cron_command` (50) + `_cron_monitor` (16) are O3's, so ONE ROW OWNS THE FAMILY** (Ryan, *scope not sequence*, §3d) | OpenBolt | **−148 lines, −5 methods** — DERIVED, not the old "−~80/−3": `_cron_monitor` 16 · `_cron_command` 50 · `_cron_find` 20 · `_cron_add` 37 · `_cron_list` 25. **Units agree (defs = items = names).** `_fire_job` (47 ln) is a **candidate 6th** — see §3d | knot |
| **S5** | **Public methods** for `model_switch.py`'s **18** hits — the `ctx.model` facade is DROPPED, see §2b. **ARRIVAL ✅ `63fd480` (OpenBolt). Consumer OPEN (SilverBolt), now 18 + 2 `misc.py` sites + the 92-stub sweep** | SilverBolt + OpenBolt | reach **40 → 20** (AST — *corrected from 22: the 2 `misc.py` `_update_header` sites are folded in*) | lines, methods, knot — **the arrival moves the def-grep by 0 while adding 5 members, see §2c** |
| **O4** | Seal the knot's plugin-facing members; split `_elapsed`/`_elapsed_*`, `_eta`/`_eta_*`, `_tps`/`_tps_*` off the knot | OpenBolt | −~200 ln, −~10 methods, **knot shrinks** | — |
| **S6** | **Public methods** for `convo.py`'s 7 hits — the `ctx.conversation` facade is DROPPED, see §2b | SilverBolt + OpenBolt | reach **22 → 15** (AST) | lines, methods, knot |
| **S7/O5** | The turn-engine boundary: `_stream` (345 ln), `_compact` (262), `_handle_command` | **both, last** | lines, knot | — |

### 2a. S1 — RESHAPED, AND WHY THE ORIGINAL SHAPE WOULD HAVE MERGED CLEAN AND DONE NOTHING

🔴 **`ctx.notify` was approved, and it was unbuildable.** SilverBolt stopped before the first line
and measured the *call sites* rather than the accesses: **49 of 49 `app._system(...)` sites have
`app` in scope and NOT `ctx`** (96 of 99 overall are app-only). Command handlers are **module-level
functions `(app, name, arg)`** registered from inside `_register(ctx)` but **not closures over it**
(`plugins/__init__.py:165`, `app.py:5187`), so a handler cannot reach `ctx` even in principle.
⇒ `ctx.notify` would have typechecked, tested green, merged, and moved reach-through **99 → 99**.

⭐ **WHY NEITHER PHASE-1 DOC CAUGHT IT — the durable part.** Both of us counted **accesses**.
Neither asked **what is in scope at the call site**. An access proves a coupling exists; it says
nothing about what a replacement would have *available* to it. Same class as the phantom member:
we measured the thing and not the thing's **context**.
📌 **Any future "seal it behind X" step must answer *is X reachable from every call site?* BEFORE it
is scheduled** — not when someone sits down to write it.

**The shape (ruled):** a public, documented method **is** a supported API. The metric is *private*
reach-through, not "number of things named `ctx`".

> **COMMIT 1 — OpenBolt, `app.py`:** add `def system_message(self, text: str) -> None:` (body
> unchanged) plus `_system = system_message` as a one-line alias. **SUITE GREEN.**
> **COMMIT 2 — SilverBolt, `plugins/**`:** the 49 sites, one token each,
> `app._system(` → `app.system_message(`. **SUITE GREEN.**
> **COMMIT 3 — OpenBolt, later and optional:** drop the `_system` alias. **Verify by grep BEFORE
> deleting, not after.**

**Both alternatives were considered and rejected.** Changing the handler contract to
`(ctx, name, arg)` touches the registry type, `app.py:5187` and every handler in 13 plugin files —
it is its own step with its own predecessors, not something smuggled under a step scoped "no state,
no lifecycle". And `ctx.notify` *alongside* the public method for the 3 ctx-reachable sites is two
doors to one behaviour, one of them with three callers — a worse surface than either alone.

**S1 first, unconditionally.** `_system` is 49 of 99, every hit a call, zero reads, return value
unused, four lines of body. Nothing else on either list is that cheap, and until it is sealed
every later extraction has to preserve a private name that 13 plugin files depend on.

### 2b. 🔴 THE `ctx` FACADE IS DROPPED ENTIRELY — S5 AND S6 WERE UNBUILDABLE, SAME REASON

**The rule at the end of §2a paid for itself twice, before either step was scheduled.** SilverBolt
classified all **43** remaining reach-throughs by *what is in scope at the call site*, by AST at
`origin/refactor/app-decomposition`:

```
40  app only        file                  hits   app-only   ctx
 3  ctx reachable   model_switch            18       18       0   <- S5's ENTIRE target
                    convo                    7        7       0   <- S6's ENTIRE target
                    scheduler_plugin         5        4       1
                    misc / skills_plugin     6        6       0
                    settings_ui              2        2       0
                    harness/mark/themes      3        3       0
                    mcp_plugin                1        0       1
                    view_image                1        0       1
```

⇒ **`ctx.model` has ZERO reachable call sites. `ctx.conversation` has ZERO.** Both would have
typechecked, tested green, merged, and moved reach-through by **exactly 0** — the `ctx.notify` false
green again, twice more, on the **two largest remaining steps**.

⇒ **THE IDEA IS DROPPED, NOT DEFERRED.** Only **3 of 43** sites could ever call a `ctx` facade, and
all three sit inside `_register(ctx)` bodies. ⭐ **A supported surface with 3 possible callers out of
43 is not the API; it is the exception.**

**THE SHAPE THAT WORKS — public methods on the app, the S1 pattern:**

> **S5** (5 members, 18 hits, all in `model_switch.py`, which IS the model UI):
> `_connect` (5) · `_fetch_ctx_window` (6) · `_update_header` (4) · `_apply_context_length` (2) ·
> `_on_model_picked` (1) → `connect()` · `fetch_context_window()` · `update_header()` ·
> `apply_context_length()` · `on_model_picked()`
>
> **S6** (7 members, 1 hit each, all in `convo.py`): `_new_convo` · `_load_system_prompt` ·
> `_compact` · `_on_convo_picked` · `_materialise_convo` · `_resume` · `_edit` → the same names
> without the underscore.

✅ **S5 ARRIVAL LANDED `63fd480`** — five defs renamed in place, five `_x = x` aliases, **runtime**
verified (`LiteTUI.public is LiteTUI._private` for all five, one implementation). Consumer half is
SilverBolt's: 18 `model_switch.py` sites + **2 `misc.py` `_update_header` sites folded in, so reach
moves 40 → 20, not 40 → 22** + the 92-stub sweep. `run_all` EXIT 0.

🔴 **`_on_` IS NOT A "HIDDEN FROM TEXTUAL" PREFIX — AND S6 RENAMES AN `_on_*` MEMBER TOO.**
The assumption going into S5 was that giving a plain callback a public `on_` name might hand it to
the message pump. **It is the other way round.** `MessagePump._get_dispatch_methods` falls back to:

```python
method = cls.__dict__.get(f"_{method_name}") or cls.__dict__.get(method_name)
```

**The UNDERSCORED name is looked up FIRST.** `_on_model_picked` was never the framework-invisible
spelling — it was the framework's *preferred* one, and the rename moved the callback to the **second**
lookup slot. Safe here because no `Message` in this app or in Textual yields the handler name
`on_model_picked` (the only local `Message` subclass is `ticker.Changed`) — **safe, not lucky, and
checked rather than assumed.** ⚠️ **S6's `_on_convo_picked` → `on_convo_picked` inherits exactly this
question. Enumerate the `Message` classes reachable there before assuming either spelling is inert.**

⚠️ **ONE PUBLIC NAME IS NOT THE MECHANICAL DE-UNDERSCORING**, and a release query built from the
private name will get it wrong: `_fetch_ctx_window` → **`fetch_context_window`**, spelled out to
match `apply_context_length`. The other four drop the underscore and nothing else.

📌 **`@work` TAKES THE WORKER NAME FROM `method.__name__`**, so renaming a decorated def renames its
worker. Checked before relying on it: `exclusive` cancels by **group**, the groups are explicit
strings and unchanged, and nothing in `src/` or `tests/` reads `worker.name` — every consumer reads
`.group` (`app.py:2973`, `app.py:3047`, `test_cron_wiring:267`). The name reaches logs only. **Real
behaviour change, no observer.**

🔴 **CORRECTED 02:20 — THIS PARAGRAPH SAID S5/S6 "MAY LAND IN EITHER ORDER" AND THAT LICENSED A
RED COMMIT.** The retracted claim: *"because the alias exists these may land in either order —
unlike S4, which has no alias and is strictly arrival-first."* **Wrong.** SilverBolt caught it inside
twenty minutes of it landing.

**THE ALIAS PROTECTS THE APP'S OWN CALL SITES, NOT THE CONSUMER'S.** `_system = system_message` keeps
`self._system(...)` working inside `app.py`, and `_jobs` keeps ~12 `self._jobs` uses working. That is
what makes the ARRIVAL commit green **standing alone**, and what lets the alias deletion be a
separate third commit later. **It says nothing about which side may land first.** A consumer calling
`app.system_message(...)` would have raised `AttributeError` before OpenBolt's commit 1 just as
`app.jobs` does now.

⭐ **THE CORRECT DISCRIMINATOR — ONE GREP, NO MEMORY OF WHAT AN ALIAS IS FOR:**
**DOES THE TARGET NAME ALREADY EXIST AT HEAD?**

| | |
|---|---|
| **ALREADY EXISTS** | no arrival needed at all — **the consumer lands alone.** |
| **DOES NOT EXIST** | **strictly arrival-first**, consumer waits. |

Measured at HEAD (`def <name>(` / `<name> = ` at class indent in `app.py`):

```
system_message 1   <- S1's arrival HAS landed
store          2   <- already public. THIS is why S2 landed by itself, blocking nobody.
connect 0 · fetch_context_window 0 · update_header 0 · apply_context_length 0
on_model_picked 0 · jobs 0 · mcp_dispatch 0 · new_convo 0 · resume 0 · compact 0
```

⇒ **S1, S4, S5 AND S6 ARE ALL STRICTLY ARRIVAL-FIRST.** S2 was the only step that was ever
consumer-alone, and it was so because its targets were already public.

**So: OpenBolt lands the public name + private alias; THEN SilverBolt converts. Every time.** The
alias still matters — it is what keeps the arrival commit green on its own — it is just not a licence
to reorder.

📌 **AND NOTE HOW THIS ERROR PROPAGATED,** because it is the failure mode of this whole document:
SilverBolt's report said S1 "had a compatibility window" — true of the app's call sites, which is what
he meant. **The orchestrator read it as true of the consumer's and wrote it into the plan as a
scheduling rule.** An imprecise sentence in a report becomes doctrine one hop later. **The plan is
where a worker's shorthand turns into everyone's instruction, so a claim entering it needs the
measurement attached, not the phrasing.**

### 2c. O-A IS NOT A TWO-LINE COMMIT — MEASURED, AST AT HEAD

Executable callers of each shim **outside `app.py`**:

```
_read_convo      7   test_convo_rename x4, test_convos x3
_convo_label     3   test_convo_rename x3
_list_convos     2   test_lazy_convo x1, test_resume_identity x1
_persist_error   1   test_convos_picker x1
_fmt_size        0   DEAD — deletable with no edits
_write_record    0   DEAD — deletable with no edits
remaining PRODUCT (src/) callers: NONE
```

⇒ **Two are free. The other four need 13 test call sites across 5 files updated in the same commit**,
or the suite goes red. *"TESTS ONLY"* was correct and is the good news; **"pure deletion" is the part
that needed a number attached.** All 13 are mechanical — but it is not the two-line commit the row
implied, and the person writing it should know that before they sit down.

📌 `_list_convos` reads **2** here against the orchestrator's earlier **3**: the third was the
`convo.py` docstring line, fixed at `e364fd3`. **Same phantom, now gone at the source** — and this is
the third count in one shift that was true when written and false two commits later.

#### ✅ DISCHARGED at `b1dd935` — and every count above held, re-measured at `be423af`

The 7 / 3 / 2 / 1 / 0 / 0 split was still exact one week and five commits later, and the 13 landed
on the nose. **The one line above that is wrong is `remaining PRODUCT (src/) callers: NONE`** — the
census scope was *outside `app.py`*, and `app.py` is `src/`. `_read_convo` had exactly one product
caller, `_resume`, which this commit repointed at `ConversationRepository.read`. *The scope was in
the heading and not in the claim, so the claim reads broader than what was measured.*

🔴 **THE ROW PROMISED `−6 methods` AND THE TRACKED GREP MOVED `−4`. Both are right; the unit was
never stated.** Derived by diffing the AST class bodies at `be423af` vs the commit:

| unit | delta | why it differs |
|---|---:|---|
| distinct **names** deleted | **−6** | what this row meant |
| **class-body items** *(excluding the class docstring)* | **−7** | `_persist_error` is ONE name and TWO defs — a `@property` and its `@setter` |
| `grep -cE '^    (async )?def '` | **−4** | the other three are `_x = staticmethod(...)` **assignments**, which a def-grep cannot see |
| `app.py` lines | **−19** | |

⇒ **The headline metric for this whole task is blind to alias-shaped members.** Any future row that
deletes or adds aliases must say which unit it is promising, or it will read as under-delivery
(here) or over-delivery (a row that adds aliases and claims no method growth).

⚠️ **AND THE SECOND UNIT NEEDS ITS OWN QUALIFIER — demonstrated on this very number, within the
hour.** SilverBolt read *"AST class-body items"* at `e6ad763` as **141**; the count above is **140**.
Neither is wrong: `len(cls.body)` counts the **class docstring** as an item and the count above
excludes it. Node types at `e6ad763`: `FunctionDef 115 · AsyncFunctionDef 12 · Assign 10 ·
AnnAssign 3` = **140**, `+ Expr 1` (the docstring) = **141**. **The deltas are unaffected** — both
sides of a delta come from one instrument — but *two people naming the same metric one commit apart
disagreed by one for a reason neither had stated.* **Write "excluding the class docstring" beside it,
every time.**

📌 **`five`/`six` are both correct and name different sets:** five of the six had **zero** product
callers; `_read_convo` was the sixth and had one. And the 13 sites are **11 calls + 1 setter
assignment + 1 alias definition** — "13 call sites" over-counts calls by two.

⚠️ **TWO SHIMS OF THE SAME SHAPE SURVIVE AND ARE NOT IN ANY ROW:** `_convo_title` and `_flatten` are
`staticmethod(ConversationRepository.…)` exactly like the three deleted here. They were never in the
six, so they were never in the 13. Measured at `be423af`: `_convo_title` **5** outside-`app.py` sites
(`test_convos` ×3, `test_convo_rename` ×2); `_flatten` **5** (`test_convos` ×4, plus
`test_tool_context_wiring:47` as a **kwarg**, not an attribute — a grep for `._flatten` misses it)
**and 2 live product callers inside `app.py`**. Whoever takes them owns those 2 product edits too.


**WHAT THE RESHAPE COSTS: nothing.** Same members, same counts, same order, same predecessors, same
arrival-first law — only the name the plugin calls. `43 → 40 → 22 → 15` is unchanged. What it
prevents is **two commits that land clean and move nothing**.

📌 **A stale count, self-reported:** the S5 row said **20** hits; it is **18**. The 20 counted two
`_system` hits that S1 has since converted — true when written, stale two commits later. Same as the
orchestrator's `grep` figure of 46 (AST: **43**) and the `convo.py` docstring. **A count is only true
at a ref.**


---

## 3. 🔴 THE ONE PLACE WE WRITE THE SAME FILE — S3, AND THE ORDER IS NOT NEGOTIABLE

**THREE** methods move **out of `app.py` into the plugin that already owns the feature.** Eight were
listed; five left, each for a *different* named reason (§3b, §3c, and Ryan's cron ruling). Each of
the three is reached exactly once, by exactly that plugin — **verified per site at the ref, not
inherited** — and **each writes nothing at all**:

| method | lines | owning plugin | writes | net reach |
|---|---:|---|---:|---:|
| `_tool_view_image` | 44 | `view_image.py` | **0** | 0 |
| `_start_mark` | 31 | `mark_plugin.py` | **0** | 0 |
| `_mcp_server_names` | 22 | `settings_ui.py` | **0** | **−1** |
| **TOTAL** | **97** | | **0** | **−1** |

| removed from S3 | lines | why | rule |
|---|---:|---|---|
| `_inbox_monitor` | 82 | writes `seat.model` + `_seat_started`; owning plugin disclaims the seat | §3b |
| `_on_settings_saved` | 65 | writes **5** fields, **4 public** and read at **26 plugin sites**; owning plugin disclaims it | §3c |
| `_cron_command` | 50 | O3 owns the `_cron`/`_cron_*` family | Ryan, scope-not-sequence |
| `_cron_monitor` | 16 | same family | Ryan |
| `_register_custom_themes` | 11 | writes nothing — leaves **only** because its sole `app.py` caller is `_on_settings_saved`, which stays | §3c |

> **COMMIT 1 — SilverBolt: the body ARRIVES in the owning plugin. `app.py` untouched. SUITE GREEN.**
> **COMMIT 2 — OpenBolt: `app.py` loses the method. SUITE GREEN.**

**Green at BOTH commits is the property, not a formality** — it is what keeps the branch bisectable
for everyone else while a two-seat move is half-done.

**It cannot be the other way round.** The moment `app.py` loses `_inbox_monitor`, the plugin that
calls `app._inbox_monitor` breaks, and the suite is red *between* the two commits — which
destroys bisectability for everyone else on the branch. Arrival-first keeps every intermediate
commit green.

Per method or batched is SilverBolt's call. **The order is not.**

### 3a. 🔴 S3's ROW IS RE-DERIVED, AND ITS REACH FIGURE HAS THE WRONG SIGN

The row publishes **reach 50 → 42**, a **−8** built by counting the call site each plugin loses.
**It does not count the sites the RELOCATED BODY creates.** Every `self._x` inside a moved method
becomes `app._x` **from inside the plugin** — which is a reach-through, by the same definition the
row is measured in. Measured at `da6fe6b`, unit = **AST call SITES** (not distinct members, per §2c):

| method | ln | removed | added | **net** | owner |
|---|---:|---:|---:|---:|---|
| `_mcp_server_names` | 22 | 1 | 0 | **−1** | `settings_ui.py` |
| `_register_custom_themes` | 11 | 1 | 0 | **−1** | `themes_plugin.py` |
| `_tool_view_image` | 44 | 1 | 1 | 0 | `view_image.py` |
| `_start_mark` | 31 | 1 | 1 | 0 | `mark_plugin.py` |
| `_cron_monitor` | 16 | 1 | 1 | 0 | `scheduler_plugin.py` |
| `_on_settings_saved` | 65 | 1 | 3 | **+2** | `settings_ui.py` |
| `_inbox_monitor` | 82 | 1 | 4 | **+3** | `harness_plugin.py` |
| `_cron_command` | 50 | 1 | 6 | **+5** | `scheduler_plugin.py` |
| **TOTAL** | **321** | **8** | **16** | **+8** | |

⇒ **AS WRITTEN, S3 MOVES REACH-THROUGH THE WRONG WAY BY 8 SITES.** Two of the eight are clean
(**−1** each), three are neutral, and three make the metric worse. **The row's premise held** —
each of the eight is reached exactly once, by exactly its own plugin, verified per site — so the
**−8 removal is real; the +16 addition was simply never counted.**

**LINES AND METHODS ARE UNCHANGED AND ALL THREE UNITS AGREE HERE:** −321 lines (the eight bodies
sum to exactly 321, re-verified), and **−8 by every unit — `def`s, class-body items, and names —
because none of the eight is an alias.** That is worth stating rather than assuming: it is the case
§2c warns about *not* biting.

🔴 **AND THAT TABLE SUMS THE MEMBERS AS IF THEY WERE INDEPENDENT. TWO OF THEM ARE NOT.**
`_on_settings_saved` reaches `app._register_custom_themes`, and **both were S3 members bound for
DIFFERENT plugins.** That one site's classification depends on whether the *other* member moved:

- `_register_custom_themes` **stays** → the site is plugin → app-private = **counted**
- `_register_custom_themes` **moves** → the site is `settings_ui.py` → `themes_plugin.py` =
  **a plugin→plugin edge, which the reach metric cannot see in either direction**

⇒ **The five-method scope scores BEST on the quoted metric (−1) precisely BECAUSE one of its
couplings stopped being measurable.** Dropping the pair scores the same **−1** with nothing hidden.
**A metric that rewards moving a dependency out of its own field of view is the false green this
plan exists to prevent** — and here it would have paid a bonus for it.

| S3 scope option | n | lines | NET (metric) | invisible plugin→plugin | app→plugin |
|---|---:|---:|---:|---:|---|
| five, as first ruled | 5 | 173 | **−1** | **1** | none |
| drop `_on_settings_saved` only | 4 | 108 | −2 | 0 | 🔴 **app.py → `themes_plugin.py`** |
| drop `_register_custom_themes` only | 4 | 162 | **+1** | 0 | none |
| **drop the PAIR (adopted)** | **3** | **97** | **−1** | **0** | **none** |

⚠️ **A THIRD ARM I HAD NOT MEASURED AT ALL: what stays behind and reaches IN.** Every pass before
this one asked what a relocated body reaches *out* for. None asked whether anything still in
`app.py` calls the member that left — which would force `app.py` to reach **into** a plugin, the
reverse direction and the worse one. Measured for all eight: **exactly one member has an `app.py`
caller left behind**, `_register_custom_themes`, and that caller is `_on_settings_saved`.

📌 **A PUBLIC ACCESSOR MAKES A PRIVATE READ FREE, and two steps already paid for four of these.**
`self._system` → `app.system_message` (S1) and `self._update_header` → `app.update_header` (S5)
cost nothing after the move, and so do `app._jobs`/`app._mcp_dispatch` via S4's properties. The
first pass of this measurement counted all four as new reach-through and read **+12**; resolving
them against the class gives **+8**. ⚠️ **And the property detector initially missed `jobs` and
`mcp_dispatch` because it required a one-statement body — their DOCSTRINGS are `body[0]`.** Same
docstring-as-a-body-item off-by-one as the class-member disagreement in §2c, in a second instrument,
the same day.

### 3b. ✅ `_inbox_monitor` — THE DEFERRED DECISION, MADE: IT DROPS OUT OF S3 AND REJOINS O4

§3 deferred this deliberately: *"if moving it whole drags shared state into `harness_plugin.py`, it
drops out of S3 and rejoins O4 — decided by measurement at the time."* Measured at `da6fe6b`:

**IT IS NOT IN THE KNOT, and the first instrument that said it was, was wrong.** Union-find over
co-written attributes puts it in a 43-method component — **but `__init__` writes 44 distinct
attributes and glues nearly the whole class together.** Excluding the constructor, the largest
component is **11** and **`_inbox_monitor`'s component is `{_inbox_monitor}` — size 1.** ⭐ **A
universal connector is not a coupling.** Any knot number quoted without saying whether `__init__`
is in it is unusable.

**IT MOVES ANYWAY — but on the STATE, not the knot:**

| what it writes | who else | verdict |
|---|---|---|
| `app.seat.model` (borrowed-object store) | written by `__init__`, **read by 4 app methods** | 🔴 **`harness_plugin.py`'s OWN DOCSTRING declares this host-owned:** *"Seat CONSTRUCTION stays host-owned in `__init__` (a core ordering guarantee — monitors must never race the seat's existence)."* **The plugin says it does not own `seat`.** |
| `app._seat_started` | written by `__init__`; **read by NOTHING in `src/`** — only `tests/test_no_fleet_registration.py:244` | a write-only flag with no in-app consumer; the plugin could own it, but nothing is gained |

Plus it reaches **`app._sync_fleet_identity`**, which mutates `self.conversation[0]["content"]` and
was **permanently withdrawn from movement in §5c**. So the body reaches into the conversation-
mutation path that is app-owned by ruling.

⇒ **VERDICT: `_inbox_monitor` LEAVES S3 AND REJOINS O4.** It costs **+3 reach**, it writes state the
owning plugin explicitly disclaims, and it depends on a permanently app-owned mutator. S3 becomes
**seven methods, 239 lines**, and its net reach becomes **+5**.

🔬 **AND THE DECORATOR IS *NOT* THE BLOCKER — PROVEN BY RUNNING IT, WITH A NEGATIVE CONTROL.**
`_inbox_monitor` and `_cron_monitor` are both `@work`, and §5's "WHAT CANNOT MOVE" lists `@work`
workers as framework-bound. **That entry is too broad.** `textual.work`'s wrapper only does
`self = args[0]; assert isinstance(self, DOMNode); self.run_worker(...)` — so a **module-level**
plugin function `async def inbox_monitor(app, ...)` decorated with `@work` satisfies the contract.
Ran it: the worker executed, `state=SUCCESS`, `group` preserved; the negative control (first arg a
`str`) raised `AssertionError`, **so the probe discriminates.** ⇒ `_cron_monitor` is genuinely
movable (net 0), and `_inbox_monitor`'s verdict rests on the state alone — which is the honest
place for it to rest.

### 3c. ⭐ THE ONE RULE THAT DECIDES EVERY S3 MEMBER: **A METHOD THAT WRITES APP STATE CANNOT MOVE**

Every verdict above was reached case by case. Measured across all eight, they collapse into one
criterion with **no exceptions and no borderline cases** — the three survivors write **zero**
attributes and the two disqualified-on-state members write **seven between them**:

| member | writes | verdict |
|---|---:|---|
| `_mcp_server_names` · `_tool_view_image` · `_start_mark` · `_register_custom_themes` · `_cron_command` · `_cron_monitor` | **0** | pure reads — **nothing to own, nothing to drag** |
| `_inbox_monitor` | 2 | `seat.model` (borrowed store), `_seat_started` |
| `_on_settings_saved` | **5** | `settings`, `theme`, `tools_enabled`, `thinking_level`, `_reasoning_ignored_warned` |

**Three independent disqualifiers, and each removed member fails exactly one:**
① **writes app state** → `_inbox_monitor`, `_on_settings_saved` · ② **belongs to another row's
family** → the two cron members (Ryan: *scope, not sequence*) · ③ **has an `app.py` caller left
behind** → `_register_custom_themes`, **which is derivative — it only bites because its caller is
disqualified by ①.**

#### `_on_settings_saved` — FLAGGED BY RYAN, AND IT LEAVES. IT IS WORSE THAN `_cron_command`.

Not because of the `+2`. Three reasons, in ascending order of force:

1. **The entanglement** — its `_register_custom_themes` read is the intra-S3 edge above.
2. 🔴 **IT WRITES FOUR *PUBLIC* FIELDS, AND THE REACH METRIC ONLY COUNTS PRIVATE NAMES.**
   `settings` (**15** plugin read sites), `thinking_level` (6), `theme` (3), `tools_enabled` (2) —
   **26 plugin-tier reads of state this one method owns**, scored as **free** because none of the
   names starts with an underscore. Moving it makes **one plugin the writer of state four other
   plugins read**, and the metric records that as `+2`.
3. 🔴 **`settings_ui.py`'s OWN MODULE DOCSTRING ALREADY SAYS SO**, unprompted and before any of this:
   > *"The Settings dataclass, its persistence, and the apply-mapping (`_on_settings_saved` with its
   > deferred-list doctrine) **stay app-owned: single owner of a fact many plugins read.**"*

⭐ **TWO FOR TWO: BOTH MEMBERS DISQUALIFIED ON STATE WERE ALREADY DISCLAIMED BY THE VERY PLUGIN THEY
WERE ASSIGNED TO** — `harness_plugin.py` on the seat (§3b), `settings_ui.py` here. **The owning
plugin's docstring was a better instrument than the reach count, and it was sitting in the file the
whole time.** Read the destination before costing the move.

⇒ **And `_cron_command` was the more honest of the two: it declared its cost as `+5`. This one's
cost is mostly in fields the metric is not looking at.**

### 3d. RULING (Ryan) — CRON IS A **SCOPE** PROBLEM, NOT A SEQUENCE ONE. ONE ROW OWNS THE FAMILY.

The `S3-before-O3` hold was ruled on one arm — *S3 deletes 66 lines of cron that O3 targets*. The
other arm points the opposite way: **relocating `_cron_command` CREATES 6 reach-throughs to
`app._cron_add` / `_cron_find` / `_cron_list` / `_fire_job`, which are O3's own targets.**
**Bidirectional entanglement cannot be fixed by ordering** — either sequence has one row building
what the other unbuilds.

⇒ **BOTH cron members leave S3 for O3.** The entanglement *disappears* instead of being sequenced
around, and O3 receives the whole family at once with nothing moving underneath it.

**O3, DERIVED AT THE REF** (the old row said *"−~80 lines, −3 methods"* and was never re-measured):

| member | ln | note |
|---|---:|---|
| `_cron_command` | 50 | from S3 |
| `_cron_add` | 37 | |
| `_cron_list` | 25 | |
| `_cron_find` | 20 | |
| `_cron_monitor` | 16 | from S3 · `@work(group="cron")` — movable, §3b |
| **TOTAL** | **148** | **5 methods; defs = class-body items = names, none is an alias** |

📌 **`_fire_job` (47 ln) is a candidate 6th and the measurement says take it:** its **only** `app.py`
callers are `_cron_monitor` and `_cron_command`, and **no plugin reaches it.** Once both callers
move it has no caller left behind — the ③ disqualifier of §3c reversed into a reason *to* include
it. With it: **6 methods, 195 lines.**

### 3e. WHERE THE REMAINING **20** REACH-THROUGH SITES ACTUALLY ARE

A total is not a map, and the next row worth picking is the one holding the largest block. AST at
`4aeb5e7`, receiver pinned to `app` (see the warning below):

| file | sites | members |
|---|---:|---|
| `convo.py` | **7** | `_new_convo` `_load_system_prompt` `_edit` `_compact` `_on_convo_picked` `_materialise_convo` `_resume` — **exactly S6's target** |
| `scheduler_plugin.py` | 3 | `_cron_command` `_cron_monitor` `_handle_command` |
| `skills_plugin.py` | 3 | `_user_bubble` `_append` `_stream` |
| `settings_ui.py` | 2 | `_mcp_server_names` `_on_settings_saved` |
| `harness_plugin.py` · `mark_plugin.py` · `misc.py` · `themes_plugin.py` · `view_image.py` | 1 each | |

**20 sites across 20 DISTINCT members — one site each, with no member reached twice.** S6 holds the
single largest block at 7.

⚠️ **A SCAN THAT DOES NOT PIN THE RECEIVER IS NOT A REACH SCAN.** Counting `.attr == "_jobs"` over
the plugin tier returns **12 hits in `scheduler_ui.py`** and every one is `self._jobs` — the
**screen's own constructor parameter** (`self._jobs = jobs`, `:114` and `:446`), not `app._jobs`.
Reporting those as reach-through would have sent someone to "fix" a widget's local state. The
receiver test (`isinstance(node.value, ast.Name) and node.value.id == "app"`) is what makes the
number mean what it says — and `scheduler_ui.py` already uses the **public** `app.jobs`, exactly as
S4 intended.

---

## 4. EXPLICIT PREDECESSORS — dependencies as rows, not prose

A dependency mentioned once in a paragraph is a dependency someone executes out of order.

| this step | cannot start until | why |
|---|---|---|
| **O4** (seal the knot's plugin-facing members) | **S5 complete** | `_update_header`, `_fetch_ctx_window`, `_connect` are frozen by `model_switch.py` until **public methods** replace those **18** hits. **The dependency SURVIVES the §2b reshape:** renaming them public does not unfreeze the internals structurally, but it removes the plugin's dependency on the PRIVATE NAME, which is what blocks him. He can then move `_connect`'s body into a service and leave `connect()` delegating — exactly what S1 did for `_system`. |
| **OpenBolt's alias cleanup** (delete the 6 `ConversationRepository` shims) | **S2 complete** | `plugins/convo.py` is the last caller keeping the shim alive |
| **S3 commit 2** (deletion) | **S3 commit 1** (arrival) | §3 — otherwise the suite is red between commits |
| **S7 / O5** (`_stream`, `_compact`) | **O4 complete** | both are inside the knot; boundary work before the knot splits is guesswork |
| everything that restructures `app.py` | **T069 in THIS BRANCH** — `git merge-base --is-ancestor origin/docs/adr-extraction HEAD` | ✅ **ancestor → DISCHARGED** (branch `330662b`, `main` `ff4f122`). Re-run the query; do not trust this cell. |
| this branch | **not behind main** — `git rev-list --count HEAD..origin/main` | ✅ **0.** Base moved by MERGE, not rebase. |

🔴 **A PREDECESSOR ROW CARRIES THE QUERY, NEVER AN ADJECTIVE.** This row previously read
*"✅ discharged — `9108042` + `b45863e`, pushed"*. **Pushed was true; discharged was false** — the
commits existed on `docs/adr-extraction`, which was not merged into anything. `merge-base
--is-ancestor` returns a *status code*; "pushed", "done" and "landed" return a feeling.
📌 Three artifacts hit that same missing distinction in one night: a branch-only handoff invisible
to a sweep of `main`, a 1,134 baseline that was correct for an unmerged branch, and this row.
**The word was wrong because the format accepted either word.**

⚠️ **MERGE IS NOT REBASE, and only one of them is forbidden here.** A merge adds a commit and
rewrites nothing, so it is safe while another seat has work in this tree. A rebase rewrites history
under whoever is not running it. **Merging `origin/main` into a shared branch needs no permission;
rebasing it is never allowed.**

**S1, S2 and O0 have no predecessors** and may run in any order or in parallel.

🔴 **S4 WAS LISTED HERE AND THAT WAS WRONG — CORRECTED 2026-08-24 02:05.** S4 has a HARD
predecessor: `app.jobs` raises `AttributeError` until OpenBolt's properties land. ⚠️ **This
originally added "unlike S1, which had a compatibility window" — CORRECTED, see §2b: S1 was equally
arrival-first. The alias protects `app.py`'s own `self._system(...)` calls, never the plugin's.**
So S4's plugin-side edits are broken from the instant they change until arrival lands. **This line
actively licensed a knowingly-red commit on a shared branch**; SilverBolt refused to make one and
was right to. **S4 is strictly arrival-first: OpenBolt lands the two properties, THEN the 3 call
sites convert.**

### §5a — `jobs` IS SHARED MUTABLE STATE, NOT A READ

The row above said *"read-only"* through eight revisions of this plan and it is false:
`plugins/scheduler_plugin.py:25` hands `app._jobs` to `scheduler_ui._apply_job_edit`, which does
`jobs.remove(job)` / `jobs.append(...)` and then persists. **A property returning a copy would
silently discard every edit made in the calendar UI**, and documenting it as read-only would put a
false contract in a docstring.

⭐ **The classification error, in SilverBolt's own words: he sorted the members by ACCESS KIND
(read vs call) and concluded "reads are safe to expose read-only". A READ OF A MUTABLE OBJECT IS A
WRITE CHANNEL.** Counting reads does not tell you what the read is FOR. Both `_jobs` and
`_mcp_dispatch` stay private; the properties are pure additions beside them, no aliases, nothing to
delete later. `mcp_dispatch` genuinely is read-only — its only caller does `.get(name)`.

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
| **O-A** | ✅ `b1dd935` — delete the six `ConversationRepository` shims | **6 names, −4 `def`s, −19 ln.** Reach already 0 via S2. **Unit note in §2c.** |
| **O2** | ✅ `22a7834` — 8 of the group members lifted to `appsvc`. `_glassbox`/`_glassbox_*` · `_load`/`_load_*` · `_append`/`_append_*` (part) · `_store`/`_store_*` | −128 ln, **−8 methods.** Knot untouched. |
| ~~O2: `_sync`/`_sync_*` (71 ln)~~ | **WITHDRAWN — the group was never stateless. See §5c.** | — |
| **O3** | remaining `_cron`/`_cron_*` → `CronService`; the framework member delegates | −~80 lines, −3 methods |
| **O4** | `_elapsed`/`_elapsed_*` (4) · `_eta`/`_eta_*` (4) · `_tps`/`_tps_*` (3) off the knot behind one state object | −~200 ln, −~10 methods, **first step that shrinks the KNOT** |
| **O5** | `_stream` (345 ln) — with S7 | high risk, last |

### 5c. 🔴 THE `_sync`/`_sync_*` GROUP WAS NEVER STATELESS — WITHDRAWN FROM O2, AND THE REASON IS NOT THE ONE I GAVE

O2 dropped `_sync_seat_identity` and `_sync_fleet_identity` from the batch and recorded the reason as
**address coupling** — their tests assert them BY NAME against `app.py`'s source. That reason is true
and it is not the load-bearing one. Address coupling is surmountable: a gate can be re-pointed.

**The load-bearing reason, measured at `e6ad763` with the CORRECTED write detector:**

| member | writes | shape |
|---|---:|---|
| `_sync_seat_identity` | 2 | `seat.agent_id = …`, `seat.error = …` — attribute stores on a **borrowed mutable object** |
| `_sync_fleet_identity` | 1 | `self.conversation[0]["content"] = fixed` — a **SUBSCRIPT store** |
| `_fleet_identity_sentence` | 0 | genuinely stateless (reads `self.seat` only) |

⇒ **Two of the three real members mutate state, so neither belongs in `appsvc` at all** — that module
is for helpers that take `app` and read it. The group's 71 lines were never a clean-lift candidate.

🔴 **THIS IS THE THIRD INSTANCE OF ONE CLASSIFIER DEFECT, AND THE FIRST TWO WERE ALREADY WRITTEN
DOWN.** `_sync_fleet_identity`'s subscript store is the *same shape* as `app._gb_last[ch] = now`,
which scored `glassbox` as stateless in O2, which is the *same shape* as SilverBolt's `jobs`
correction before that. **Three sightings, one rule, and the rule was recorded after the first one.**
Knowing a rule and having your instrument apply it are separate facts about separate objects — the
fix belongs in the detector, not in a note beside it.

**WHAT THIS CLOSES.** The `_sync` row is withdrawn, not deferred: there is no later commit that makes
a mutating method a stateless lift. If these two ever leave `app.py` it is as a **seat/identity
service that OWNS the state**, which is O4-shaped work, not O2-shaped.

**AND THE GATES ARE SOUND — audited, not assumed.** Before recommending anything about them:

| gate | addressing | verdict |
|---|---|---|
| `test_seat_rebind:271` `src.split(f"def {fn}")` for `_new_convo`, `_resume` | literal split | **1 occurrence each, no prefix collision** — no sibling method starts with either name |
| `test_seat_rebind:293` AST `FunctionDef == "_sync_seat_identity"` | AST | **1 def, and it walks the AST precisely so a docstring quoting `registered = False` cannot fool it** |
| `test_seat_identity:155` `count("self._sync_seat_identity()") >= 2` | string count | **string 2 = AST calls 2** — no prose inflating it |
| `test_fleet_identity:127,142` `"_sync_fleet_identity()" in …` | literal | 1 def, both call paths real |

⇒ **There is nothing to repair in them, so there is no re-scoping commit either.** Re-pointing a
sound gate to enable a move that is now withdrawn would be work whose only product is risk to four
assertions that pin a bug which shipped.

📌 **`harness.py:294` NEEDS NO EDIT, AND THE REASON IS VISIBLE IN ITS OWN TENSE.** It reads
*"app.py's `_sync_seat_identity()` **used to** deregister the old row…"* — history, explaining why
`Seat.rebind()` exists. The method still lives in `app.py` and now calls `rebind`. **A grep for the
name returns 1 because the prose QUOTES it in order to retract it** — the exact trap
`test_seat_rebind:280` documents one file away: *"a string grep cannot tell an instruction from its
own retraction."* Raised twice, checked twice, correct both times.

### WHAT CANNOT MOVE, AND WHY

**34 framework-bound members** stay on the class by contract with Textual — `compose`, `on_*`
(message pump), `action_*` (bindings), `watch_*` (reactives), `get_system_commands`,
`check_action`, and ~~`@work` workers~~. The most any of them becomes is a one-line delegation.

🔴 **CORRECTED — `@work` DOES NOT BELONG ON THAT LIST, and the over-broad entry would have frozen
two of S3's members for no reason.** `textual.work`'s wrapper does only
`self = args[0]; assert isinstance(self, DOMNode); self.run_worker(...)`, so a **module-level**
function `async def x(app, ...)` under `@work` satisfies the contract completely. **Proven by
running it, not by reading it:** the worker executed with `state=SUCCESS` and its `group` intact,
and the negative control — same decorator, first argument a `str` — raised `AssertionError`, so the
probe discriminates. ⚠️ **`on_*` on that list needs the same scepticism from the opposite
direction:** the message pump looks up `_on_x` **before** `on_x` (§2b), so the underscore never made
a member framework-invisible. **Neither prefix nor decorator settles a member's status — the
framework's actual contract does, and the only way to know it is to run it.**

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
- **Merging `origin/main` into this shared branch needs no permission. Rebasing it is never
  allowed.** A merge adds a commit and rewrites nothing, so it is safe while another seat has work
  in the tree; a rebase rewrites history under whoever is not running it. The rule was never that
  someone holds a lever — it is that nobody rewrites shared history.
- `prompts/systemprompt.md` stays fenced and untouched.
- Gates per commit: full suite green against **your base's own collected count**, ruff no worse
  than base (`ruff check src` = 220 on `ab8b4a9`), diff scoped to the files the step names.

---

## 8. 🔴 MEASUREMENT PROTOCOL — MANDATORY. NO METRIC COMES FROM THE WORKING TREE.

**Two seats share this worktree and the close condition is a tree-wide count. Those two facts are
incompatible, and the failure flatters you:**

```
reach-through measured from the working directory   92 hits / 28 members
reach-through measured at HEAD                      99 hits / 34 members
                                                    ^ the 7-hit gap was the OTHER SEAT'S
                                                      UNCOMMITTED WORK
```

A number going down is exactly what this task exists to produce, so a contaminated reading looks
like a **win** and nothing about it looks wrong. It was caught only because the drop was larger than
the change could possibly explain.

1. **METRICS FROM COMMITTED STATE ONLY.**
   ```bash
   git archive HEAD src/litetui | tar -x -C <tmp>   # then count against <tmp>
   ```
2. **SUITE RUNS IN A DETACHED VERIFY TREE**, never in the shared one — a full run here is a reading
   of *both* working copies:
   ```bash
   git worktree add --detach .worktrees/<name>verify HEAD   # copy in ONLY your change
   ```
3. **VALIDATE THE INSTRUMENT IMMEDIATELY BEFORE THE RUN YOU INTEND TO QUOTE — not when you build
   it.** Confirm the verify tree really is HEAD plus your one change: `git rev-parse HEAD:src` in
   both places must match. *A verify tree you have not checked is just a second place to be wrong.*

   🔴 **AMENDED after it nearly fired (2026-08-24).** A verify tree built at `d7e13d0` was
   re-checked before its run and disagreed — `e9feb736df7d` vs `17463f9bcfd7` — because the other
   seat had committed S2 in the interval. **The stale tree would have produced a GREEN FOR A TREE
   THAT NO LONGER EXISTED**: nothing errors, the suite passes, the number gets quoted.
   ⇒ **In a shared tree the instrument goes stale WHILE YOU USE IT.** A validation done at build
   time certifies the tree you built, not the tree you are about to measure — and the gap between
   those two is exactly when the other seat commits.
4. **EVERY METRIC STATES WHERE IT WAS TAKEN** — "at HEAD `<sha>`" or "in verify tree from `<sha>`".
   **A bare number is inadmissible.** This whole task has been a run of numbers that were each
   correct for a tree nobody named.

---

## 9. 🔴 NOTATION: WRITE `_x`/`_x_*`, NEVER `_x_*` ALONE

**Two seats, no communication, hours apart, built the SAME broken matcher from ONE line of this
document.** The groups were written `_sync_*`, `_glassbox_*` — with a trailing underscore — and both
readers transcribed the wildcard into `name.startswith("_glassbox_")`, **which cannot match a method
called exactly `_glassbox`**. `_glassbox` (27 ln) and `_append` (4 ln) vanished from both counts.

⚠️ **A defect two independent readers reproduce is a defect in the WRITING, not in the readers.**
`_glassbox_*` reads as "the glassbox family" to a human and compiles to "excludes `_glassbox`" in a
matcher. Same family as a text gate counting the prose *about* a thing: the notation and the set it
denotes are not the same object.

⭐ **AND THE TRAP THAT ALMOST CLOSED IT WRONG: WHEN TWO SEATS AGREE ON A SURPRISING NUMBER, THAT IS
NOT CORROBORATION.** Both of us got 14/189, both concluded the PLAN was stale, and each was more
confident *because* the other matched. Independent agreement is evidence only when the INPUTS are
independent — a shared document makes them anything but. Two matching wrong answers are more
persuasive than one and no more correct.
