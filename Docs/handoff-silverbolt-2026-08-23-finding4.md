# Handoff — SilverBolt, LiteTUI, finding 4, 2026-08-23

**Supersedes** `handoff-silverbolt-2026-08-23.md` — verified present on `main`
(`git cat-file -e main:Docs/handoff-silverbolt-2026-08-23.md`). That one covers the seat/mcp
round; this one covers finding 4 only.

🔴 **WHICH REF THIS IS ON.** Committed on `refactor/turn-engine`, which is **NOT merged**. A
sweep of `main` will report this file ABSENT and that is correct, not a loss.

```bash
git -C C:/Projects/LiteTUI show refactor/turn-engine:Docs/handoff-silverbolt-2026-08-23-finding4.md
git -C C:/Projects/LiteTUI branch -a --contains 8fa780f
```

---

## 1. In flight

**NOTHING.** Checked, not assumed:

```bash
git -C C:/Projects/LiteTUI/.worktrees/turn-engine status --porcelain | wc -l   # 0
```

Two commits on `refactor/turn-engine`, both complete and both tested:

| sha | what |
|---|---|
| `46b60a0` | ConversationRepository — the store leaves app.py |
| `8fa780f` | TurnEngine — the turn's decisions leave; rendering stays |

Branch is **not merged and not pushed.** T061 is on the board at `building`.

## 2. Owed — split by owner

### MINE
Nothing outstanding on finding 4 as scoped. The class is still a god object (§6) but the
assignment was TurnEngine + ConversationRepository, and both exist.

### THEIRS
| seat | what |
|---|---|
| **Sentinel** | T060 verdict (still `reviewing`); whether to merge `refactor/turn-engine`; the two gate corrections in §5 |
| **OpenBolt** | both remaining suite failures are his by domain — the autoscroll flake AND `test_tool_cancel`, which is a **second, undocumented flake at ~80% failure in isolation** (§4) |
| **BoldChip** | `/goal` + `/loop`. The shape-delta note he is owed is §7 of this file |

### RYAN'S
- 🔴 **Every push.** Nothing is pushed anywhere. `git rev-list --count @{u}..HEAD` on `main` was
  **21** when this branch was cut.

## 3. Absent by decision — each with the gate that defends it

| Deliberately not done | The gate |
|---|---|
| `_stream` and `_compact` NOT relocated | Measured: `_stream` makes **58 UI attribute accesses in 375 lines** (AST). It is the view. Moving it behind a host reference swaps 58 direct calls for 58 indirect ones and adds a hop to the hottest loop. Sentinel scoped the lift as *"as far as TurnEngine extraction requires"* and forbade redesign |
| `_execute_tool` NOT moved | It needs `push_screen_wait` for the CONFIRM dialog and the host's active tool profile — authorization belongs where the UI lives. Keeping it still means the door literally never moved |
| `chat_request` / `compact_request` NOT merged | They are not the same request (§7.4). Merging is a redesign. `tests/test_turn_engine.py` pins the `stream_options` difference so the merge must be deliberate |
| `_contextualise_tool_result` NOT moved | 86 lines, cleanly movable, and I ran out of scope rather than out of ability. It is the **next best single extraction** — it needs client + settings + store passed explicitly and nothing else |
| The `test_tool_cancel` flake NOT fixed | Flakes are OpenBolt's by Sentinel's ruling, and the fix would change what the test DOES, which Sentinel scoped out of my assignment |
| Store properties NOT collapsed into call-site rewrites | **22 references outside app.py** — tests — read and assign `app.convo_id` directly. The properties are the public surface, not scaffolding |
| My worktree NOT removed | `git worktree remove --force` FOLLOWS Windows junctions and deletes the target's contents (264 GB lost that way 2026-08-01). Scan for junctions first. It is also the only home of this file |

## 4. Caveats riding every green line

- ⚠️ **`2 failed, 1069 passed` is ONE RUN.** A single green does not establish a clean failure
  set — that is exactly the mistake Sentinel made and I corrected earlier today.
- ✅ **The failure COUNT is unchanged from baseline and the delta is fully accounted:**
  `b604bc2` = 2 failed / 1041 passed / 255s. This branch = 2 failed / 1069 passed / 244s.
  `1069 = 1041 + 28`, exactly the new tests.
- 🔴 **`test_tool_cancel::test_cancel_kills_the_whole_tree_and_the_turn_survives` is a SECOND
  flake and it was on nobody's list.** n=5 in isolation on my branch: **4 of 5 FAILING.** Root
  cause is the test's *instrument*: `tests/test_tool_cancel.py:46` locates its probe with an
  **unfiltered** `Get-CimInstance Win32_Process` enumeration against a 30s subprocess budget;
  the fast failures land at 31.4s. The thing under test — tree-kill — is never reached.
- 🔴 **The flakiness is LOAD-DEPENDENT, so samples are not IID.** Same three tests on the
  unmodified control took 136s, 98s, 39s, 23s, 28s, and **only the slow runs failed**
  (1/2/1/0/0). An n=20 standard must be run under comparable load or the sample lies.
- ✅ **Behaviour preservation is PROVEN for the request builders**, not asserted:
  `uv run python tools/refactor_differential.py` → *50 input combinations compared against
  b604bc2, IDENTICAL*. It lifts the original blocks out of git by anchor text and diffs the
  output dicts.
- ⚠️ **The differential covers the request builders only.** `accumulate_tool_call` and
  `autocompact_due` are covered by unit tests written from the new code plus a line-by-line
  reading of the old — **weaker evidence**, and I am saying so rather than letting one green
  cover both.
- ⚠️ **No mounted-Textual turn was ever driven.** No live model, no real stream.

## 5. My own corrections and retractions

1. **I told Sentinel the app.py-scoped gate (`_execute_tool( == 3`) would break. IT DID NOT.**
   I predicted that while planning to move `_stream`/`_compact`; I then changed the design and
   they stayed, so the callers stayed, so the gate still reads 3. It is **fragile, not wrong**,
   and retiring it has to be argued on fragility. He was writing the retirement into the record
   on the strength of my claim.
2. **I reported the package-scoped grep at 2. It is now 3, and I added the third.**
   `turn_engine.py:23` — my own docstring explaining that the grep is unreliable — is itself a
   false positive for that grep. **Writing down why the gate is wrong made the gate wronger.**
3. **My first coupling measurement used a regex and was wrong.** `\bpush_screen\b` cannot match
   `push_screen_wait`, so `_execute_tool` reported **0** UI hits when it has one. Every number
   in §3 and §7.3 is from the AST re-measurement, not that first pass.
4. **My migration made `list_all` an instance method while calling it as a static** — caught on
   the first inspection, before any test ran.
5. **I inlined a message body containing backticks and the shell ate the evidence**, while
   `liteharness send` reported success. The destroyed message was the one arguing that a
   string-based gate cannot tell code from prose. `send --help` warns about this in those words
   and I had read it an hour earlier — and had used `--body-file` for *Sentinel's* payload but
   not my own.

⭐ Three of these five are the same root: **text matched where a statement was meant.** It has
now fooled a guard, a classifier, an audit, a search, a gate written to replace a failed gate,
and a shell. Finding it repeatedly has not stopped it recurring.

## 6. The honest limit

```
                    b604bc2    now   delta
app.py lines           5329   5135    -194
LiteTUI class lines    4325   4174    -151
LiteTUI methods         126    136     +10
```

**The class is still 4,174 lines and 136 methods, and the method count went UP.** The +10 are the
store properties. 533 lines of logic now live in two focused modules and 28 tests run in 0.16s
against logic that previously needed a Textual mount — that is the real win, not the line count.
**Do not read this branch as "finding 4 is done."**

## 7. Shape-delta note — for BoldChip

### 7.1 Your fence is intact; its instrument changed
`_execute_tool` did not move, is still the one door, still the only real
`asyncio.to_thread(fn, ...)`, still reached by exactly two dispatch paths. **Check it with
`python tools/tool_door_gate.py`** (AST, package-scoped, exit 0/1) — not with either grep, for
the reasons in §5.2.

### 7.2 Renames — all old names still resolve
| was | is now |
|---|---|
| `LiteTUI._read_convo` / `_convo_label` / `_convo_title` / `_flatten` / `_fmt_size` / `_list_convos` | `ConversationRepository.read` / `.label` / `.title` / `.flatten` / `.fmt_size` / `.list_all` — **aliases kept on `LiteTUI`** |
| `LiteTUI._write_record` + record shapes | `ConversationRepository.write_record` / `record_msg` / `record_snapshot` / `record_edit` / `record_truncate` / `record_meta` |
| `self.convo_id` / `convo_dir` / `convo_path` / `_convo_pending` / `_convo_loading` / `_persist_error` | **properties** over `self.store` — read/write exactly as before |
| `TRANSCRIPT_NAME`, `CONVO_SEED_FILES` | `litetui.conversation`, re-imported into `app` |
| the `kwargs` blocks in `_stream` / `_compact` | `TurnEngine.chat_request` / `TurnEngine.compact_request` |
| the tool-slot merge in both loops | `TurnEngine.accumulate_tool_call(acc, tc) -> (idx, named_now, argued_now)` |
| `_autocompact_due`'s policy | `TurnEngine.autocompact_due(...)` — the method remains, now reads live values |

### 7.3 **`_fire_job` was not touched at all.** If your design assumed its shape, that holds.

### 7.4 The thing most likely to be "tidied" into a bug
`chat_request` and `compact_request` look mergeable and are not: compact sends **no
`stream_options`**, **no sampling overrides**, a different `max_tokens`, and a different thinking
setting. `tests/test_turn_engine.py::test_a_compaction_sends_NO_stream_options_unlike_a_chat_turn`
exists to make that merge deliberate.

## Verification block — every claim as a command

```bash
cd C:/Projects/LiteTUI/.worktrees/turn-engine
git log --oneline -3                                  # 8fa780f, 46b60a0, b604bc2
git status --porcelain | wc -l                        # 0
python tools/tool_door_gate.py                        # 1 door, 2 callers, exit 0
uv run python tools/refactor_differential.py          # 50 combos vs b604bc2, IDENTICAL
uv run pytest tests/test_turn_engine.py -q            # 28 passed, ~0.2s
uv run pytest -q                                      # 2 failed, 1069 passed — see §4
# the two failures, on an UNMODIFIED tree, to prove they are not mine:
cd C:/Projects/LiteTUI && uv run pytest \
  tests/test_tool_cancel.py::test_cancel_kills_the_whole_tree_and_the_turn_survives -q
```
