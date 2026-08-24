# T070 Phase 1 — SilverBolt (the seal half)

Read-only analysis. No file in `src/` modified. Numbers are AST-derived over
`src/litetui/plugins/**` and `src/litetui/app.py`; the script is in this session's scratchpad
(`plugin_reach.py`) and is re-runnable.

The question I was given is **what the 14 plugin files NEED — the capability, not the attribute.**

---

## 0. 🔴 THE COUNT IN BOTH BRIEFS IS A GREP COUNT, AND IT IS THREE HIGH

| | reach-throughs | distinct members | plugin files |
|---|---:|---:|---:|
| `grep -c "app\._"` (brief, and OpenBolt §4) | 102 | 35 | 14 |
| **AST, statements only** | **99** | **34** | **13** |

The three differences are all **prose inside docstrings**:

- `scheduler_ui.py:4` — *"including their (deliberately broad) access to app._jobs and _fire_job."*
- `view_image.py:6` — *"it stages it (app._pending_tool_images, agent-loop infrastructure)"*
- `skills_plugin.py:53` — *"`app._system` only mounts a widget into the chat log"*

⚠️ **`_pending_tool_images` is a PHANTOM MEMBER.** It is the 35th member in the grep and it
appears in **no executable statement anywhere in `plugins/`** — a plugin file describing app
internals in its own module docstring. A seal task driven off the grep would go looking for a
caller that does not exist.

`_system` is **49**, not 50, for the same reason. This does not change OpenBolt's conclusion —
it sharpens it: `_system` is 49 of 99, still **49%**.

*(My own first pass said 18/9/5. It tracked only `x = ctx.app` assignments and missed the
dominant binding, which is a **parameter** — `def _cmd_think(app, name, arg)`. Under-inclusive,
and it looked like a finding rather than a bug. Corrected before reporting.)*

---

## 1. WHERE THE COUPLING ACTUALLY LIVES — TWO FILES ARE 65% OF IT

```
hits  members  file
  41       6   model_switch.py     <- most COUPLED (6 members, 41 uses)
  23      14   convo.py            <- widest SURFACE (14 of the 34 members)
  10       4   skills_plugin.py
   7       3   misc.py
   5       1   glassbox_plugin.py
   5       4   scheduler_plugin.py
   2       2   settings_ui.py
   1       1   × 6 more files
```

Two different problems wearing one number: `model_switch.py` uses **few members heavily**,
`convo.py` uses **many members once**. A single API shape will not serve both.

---

## 2. ✅ OPENBOLT'S HYPOTHESIS CONFIRMED FROM THE CALLER SIDE

`_system` is **49 of 99 (49%)**, and from the plugin side it is even cleaner than his read:

- **every one of the 49 is a CALL. Zero reads.** No plugin holds it, wraps it, or passes it on.
- **every call passes text and uses the return value never** — it returns `None`.
- the method is 4 lines: `query_one("#chat-log")` → `mount(ChatMessage(..., classes="system-msg"))`
  → `_scroll_down()`.

⇒ The capability is **"emit a system notice into the transcript"**, and it needs no app.
`ctx.notify(text: str) -> None` seals 49% of the coupling with no state, no lifecycle, and no
behaviour change. **This is the first thing to build and it is not close.**

---

## 3. 🔴 THE FINDING THAT IS NOT IN OPENBOLT'S HALF: 14 OF THE 34 MEMBERS NEED NO NEW API AT ALL

### 3a. Six are ALREADY PUBLIC, hiding behind a private alias (8 hits)

| reached as | what it actually is |
|---|---|
| `app._read_convo` | `staticmethod(ConversationRepository.read)` — app.py:2565 |
| `app._fmt_size` | `staticmethod(ConversationRepository.fmt_size)` — app.py:2566 |
| `app._convo_label` | `staticmethod(ConversationRepository.label)` — app.py:2567 |
| `app._list_convos` | 1-line delegation → `ConversationRepository.list_all()` |
| `app._write_record` | 1-line delegation → `self.store.write_record(rec)` |
| `app._persist_error` | property → `self.store.persist_error` |

`ConversationRepository` is a **public module** (`litetui/conversation.py`). The aliases exist,
by their own comment, so that *"no call site changed"* when the repository was extracted —
they are a compatibility shim, and `plugins/convo.py` is now the thing keeping them alive.

⇒ **`convo.py` should import `ConversationRepository` and use `app.store` directly.** No new
API, no design work. It is a deletion, and it is what lets OpenBolt remove the aliases.

### 3b. Eight are the plugin's OWN feature, sitting in the wrong file (8 hits, 321 lines)

Each of these is reached **exactly once, by exactly the plugin that owns that feature**:

| member | lines | reached only by |
|---|---:|---|
| `_inbox_monitor` | 82 | `harness_plugin.py` |
| `_on_settings_saved` | 65 | `settings_ui.py` |
| `_cron_command` | 50 | `scheduler_plugin.py` |
| `_tool_view_image` | 44 | `view_image.py` |
| `_start_mark` | 31 | `mark_plugin.py` |
| `_mcp_server_names` | 22 | `settings_ui.py` |
| `_cron_monitor` | 16 | `scheduler_plugin.py` |
| `_register_custom_themes` | 11 | `themes_plugin.py` |

⇒ **These are not coupling to seal. They are methods to RELOCATE into the plugin that already
owns the feature.** Sealing them behind an API would enshrine the wrong home.

⭐ **This is the only step in either of our analyses that moves all three metrics at once** —
**−321 lines, −8 methods, −8 reach-throughs** — which is precisely what the brief says progress
requires, and precisely what the two prescribed extractions failed to do.

---

## 4. WHAT GENUINELY NEEDS A DESIGNED API — 40 HITS, AND IT IS NARROWER THAN 34 MEMBERS

After `_system` (49), the already-public six (8), and the relocatable eight (8), **34 hits over
19 members** remain. They group into four real capabilities:

| capability | members | hits | shape |
|---|---|---:|---|
| **Model & connection lifecycle** | `_connect` (88 ln), `_fetch_ctx_window` (19), `_apply_context_length` (55), `_update_header` (24), `_on_model_picked` | 20 | `ctx.model` — reconnect / refresh window / apply length / redraw header. All from `model_switch.py`, which IS the model UI |
| **Conversation lifecycle** | `_new_convo`, `_materialise_convo`, `_resume`, `_edit`, `_load_system_prompt`, `_on_convo_picked`, `_compact` (262 ln) | 7 | `ctx.conversation` — sits beside `ctx.store`; `_compact` is the outlier and belongs with the turn engine, not here |
| **Transcript output beyond a notice** | `_user_bubble`, `_append`, `_clear_screen` | 3 | folds into the same object as `notify()` — one transcript API, not two |
| **Read-only state** | `_jobs` (list, 2 reads), `_mcp_dispatch` (dict, 1 read) | 3 | instance attributes, never called — expose as read-only properties. Cheapest items on this list |

### The four that are genuinely hard, and they are hard for the same reason

`_stream` (**345 lines**), `_compact` (**262**), `_connect` (**88**), `_inbox_monitor` (**82**).

These are the largest methods in the file, three of them are inside OpenBolt's shared-state knot,
and a plugin calling `_stream` **is driving the turn engine**. That is not a notice, a getter, or
a lifecycle call — it is a genuine capability boundary and the only part of my half that needs
design rather than mechanics.

⚠️ `_inbox_monitor` appears in **both** §3b and here. It is relocatable *and* 82 lines *and*
knot-adjacent; whether it moves whole or gets a boundary first is a real question for PLAN.md, and
I would rather flag the overlap than pick silently.

---

## 5. PROPOSED ORDER FOR MY HALF — CHEAPEST AND MOST CERTAIN FIRST

| # | step | reach-through | app.py lines | methods | risk |
|---|---|---|---:|---:|---|
| S1 | `ctx.notify(text)`; `_system` keeps a 1-line delegation | **99 → 50** | ~0 | 0 | very low |
| S2 | Point `convo.py` at `ConversationRepository` / `app.store` | 50 → 42 | ~0 | 0 (unblocks −6 aliases) | very low |
| S3 | Relocate the eight owner-plugin methods into their plugins | 42 → 34 | **−321** | **−8** | low |
| S4 | Read-only `jobs` / `mcp_dispatch` properties | 34 → 31 | ~0 | 0 | very low |
| S5 | `ctx.model` facade (`model_switch.py`'s 20 hits) | 31 → 11 | ~0 | 0 | medium |
| S6 | `ctx.conversation` facade, `_compact` excluded | 11 → 5 | ~0 | 0 | medium |
| S7 | The turn-engine boundary: `_stream`, `_compact`, `_handle_command` | 5 → small defended set | — | — | high — after OpenBolt's knot work |

**S1–S4 take the reach-through from 99 to 31 and cost app.py nothing but deletions.** None of
them touches the knot, so none of them blocks or is blocked by OpenBolt's steps 0–3.

🔴 **What S1–S4 do NOT do, stated plainly:** they barely move the *line count* (only S3 does,
−321 of 5,218 ≈ 6%) and they do not shrink the knot at all. If the seal half were reported as
"decomposition progress" it would be the same false green the brief warns about. My half removes
**coupling**; only S3 also removes **volume**; **none of it removes shared state.**

---

## 6. FOR PLAN.md — WHERE MY HALF CONSTRAINS OPENBOLT'S

- **`_system` is not in the knot and is not framework-bound**, so S1 can land first, independently,
  and it makes every later extraction cheaper by removing the single most-depended-on private name.
- **The five members he lists as frozen-by-plugins are confirmed frozen**, and two are already
  covered by my steps: `_update_header` and `_fetch_ctx_window` free up at S5, `_connect` at S5,
  `_stream` and `_resume` not until S7. **His step 4 depends on my S5.**
- **The `ConversationRepository` aliases (§3a) are his to delete, not mine** — I remove the last
  callers at S2 and he removes the shim. Sequencing: S2 before his alias cleanup.
- **S3 moves code OUT of app.py into `plugins/`.** That is 321 lines leaving the file by a route
  his §5 does not include, and it touches app.py — so **he must own the deletions** while I own
  the arrivals, exactly as §0 of the brief requires. This is the one place our halves write the
  same file and it needs an explicit order in PLAN.md.
