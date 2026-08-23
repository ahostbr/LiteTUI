# Handoff — SilverBolt, LiteTUI, 2026-08-23

**Predecessor:** NONE in this repo — head of chain. My only earlier handoff belongs to a
different chain (`C:\Projects\docs\plans\2026-08-22-gauntlet-loop\handoff-silverbolt-2026-08-22-gauntlet-s01.md`,
LiteImage/gauntlet). If a later reader finds no successor to this file, that is the head-of-chain
blind spot, not evidence of loss.

🔴 **WHICH REF THIS IS ON.** Committed on `fix/seat-and-mcp` — a branch already MERGED into `main`,
so this file is **NOT reachable from `main`** and a sweep of `main` will report it ABSENT.
Retrieve it with:

```bash
git -C C:/Projects/LiteTUI show fix/seat-and-mcp:Docs/handoff-silverbolt-2026-08-23.md
git -C C:/Projects/LiteTUI branch -a --contains <sha>      # name the refs it lives on
```

Say the word and I cherry-pick it onto `main`; I did not, because the instruction was "commit it
on your current branch" and pushing/merging is not mine to decide.

---

## 0. Correcting the state as Sentinel holds it

He has me right on every row. Two amendments, neither a disagreement about what merged:

1. ⚠️ **"Locked env 1038 passed exit 0, failure set empty" is ONE RUN of a suite containing a
   1-in-5 flake.** By his own §4 rule — *a single green from a known flake is not evidence* — that
   line needs the same caveat he asked the rest of us to write. It is not wrong; it is unqualified.
   The honest form: *one locked-env run was clean; `test_thinking_autoscroll` is 1-of-5 in
   ISOLATION (pre-existing, `48ce704`), so an empty failure set from n=1 does not establish one.*
2. 🔴 **EVERY PATH IN MY EARLIER REPORTS IS NOW STALE.** The package move (`7bed9e1` renames,
   `ba85462` import rewrite) relocated my files. Anyone acting on my prior messages must repoint:

   | I said | it is now |
   |---|---|
   | `src/harness.py` | `src/litetui/harness.py` |
   | `src/app.py` | `src/litetui/app.py` |
   | `src/mcp_client.py` | `src/litetui/mcp_client.py` |
   | `tests/test_seat_rebind.py` | unchanged |
   | `tests/test_mcp_timeout_bounds.py` | unchanged |

   My fixes survived the rewrite — verified on `main`, not assumed:
   ```bash
   git show main:src/litetui/harness.py    | grep -c 'def rebind'          # 1
   git show main:src/litetui/app.py        | grep -c 'seat.rebind('        # 1
   git show main:src/litetui/mcp_client.py | grep -c 'def _start_reader'   # 1
   # and the defect is gone, gated on the STATEMENT not the string:
   git show main:src/litetui/mcp_client.py | python -c "import ast,sys; fn=next(n for n in ast.walk(ast.parse(sys.stdin.read())) if isinstance(n,ast.FunctionDef) and n.name=='_read_until'); print(sum(1 for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='readline'))"   # 0
   ```

---

## 1. In flight

**NOTHING IN FLIGHT.** Not shorthand for "probably nothing" — checked:

```bash
git -C C:/Projects/LiteTUI/.worktrees/seat-mcp status --porcelain | wc -l    # 0
lst run tasks action=list | grep T060                                        # status: reviewing
```

My three commits are ancestors of `main` (`04082db`, `b774fc0`, `77e997a`); verify each with
`git merge-base --is-ancestor <sha> main`.

---

## 2. Owed — split by owner

### MINE
Nothing outstanding. Finding 4 (`app.py` god object → TurnEngine / ConversationRepository) is
sequenced to me but **unassigned and unstarted** — see §3.

### THEIRS
| seat | what |
|---|---|
| **OpenBolt** | the `test_thinking_autoscroll` flake — his by domain, own commit after the rename, n=5 before and after, no sleep/retry/widened tolerance, and duplication checked first (deletion may beat repair) |
| **Sentinel** | T060 verdict (still `reviewing`); finding 4 sequencing |
| **BoldChip** | `/goal` + `/loop` design — approved in shape, not started, new worktree after tool-policy |

### RYAN'S
- 🔴 **Every push.** `main` at `b82668a` is **local to this box**. Nothing is pushed anywhere —
  not this repo, not liteharness-oss. `git rev-list --count @{u}..HEAD` is the live counter.
- The H3 LoRA A/B in **LiteImage** (different repo): wire `--lora-model-dir` through
  `buildMiniMaxVideoArgs`, same seed at 20 vs 6 steps, compare wall-clock. Mine to run on his word;
  detail in memory `minimax-h3-working-set-and-license`.

---

## 3. Absent by decision — each with the gate that defends it

| Deliberately not done | The gate |
|---|---|
| Finding 4 not started | Sentinel, explicit and repeated: *"Do not start finding 4"*; it is unassigned |
| Identity ownership untouched | **Ryan ruling 2026-08-21** — `agent_id` DERIVES from convo id (`uuid5`, `"litetui:seat:" + cid`, shipped `0852dab`). Per-process ids were tried and rejected: one conversation minted three ids in an evening and a dispatched task was silently never delivered |
| `register()`'s stale comment RETRACTED, not deleted | Deletion loses the warning and the false premise regenerates — the next reader re-derives the rejected design. Sentinel accepted this reasoning explicitly |
| Full suite NOT re-run after `77e997a` | Comment-only, proven by query: `git diff -U0 src/harness.py \| grep '^[+-]' \| grep -v '^[+-][+-]' \| grep -vc '^[+-]\s*#'` → **0** non-comment lines. Sentinel: *"running 3m48s to confirm that would be ceremony, not evidence"* |
| The autoscroll flake NOT touched | OpenBolt's by Sentinel's ruling; it is pre-existing at `48ce704` and predates all three branches |
| No live MCP stdio server test | Belongs in the e2e tier OpenBolt is building; faking one here would be a worse proof than the honest gap |
| My worktree NOT removed | `git worktree remove --force` FOLLOWS Windows junctions and deletes the target's contents (264 GB lost this way 2026-08-01). Scan for junctions first. Also: this branch is currently the ONLY home of this handoff |
| Nothing sent to OpenBolt about the by-name file check | Sentinel handed him that check to re-run himself; duplicating it as a message is narration |

---

## 4. The caveat riding every pass line

- 🔴 **`991 passed` / `1000 passed` are MACHINE FACTS, not measurements.** I ran them on **system
  python**, where `pytest-asyncio` sits at 1.3.0 **declared nowhere** (no hit in `pyproject.toml`
  or `requirements*`), and **26** suite files carry `pytest.mark.asyncio`. Read them as *"nothing
  regressed on this box"*. Do not re-run them to "confirm" — that produces a second machine fact.
- ✅ **The targeted 40 ARE ablation-clean**, and that is a control rather than a claim:
  `pytest <the four seat/mcp files> -q -p no:asyncio` → **40 passed**. Plugin removed, same result.
- ⚠️ **`1038 passed, exit 0` in the locked env is n=1** against a suite containing a 1-in-5
  isolation flake. See §0.1.
- ⚠️ **Finding 2 is proven against a stalled-pipe FAKE plus an AST guard — never a live stdio
  server.** The reproduction before the fix was real (declared 0.1s timeout, still blocked at 5s,
  never returned); the *fix* is verified structurally and by fake.
- ⚠️ **No mounted-Textual `/new` or `/resume` was ever driven.** The transition runs through the
  real seam and the real `Seat.rebind` over a faked transport; both call sites are asserted wired
  by AST, not by execution.

---

## 5. My own corrections and retractions

1. **I called the silent-zero collection crash "no signal wearing the costume of a clean run."
   Wrong — the exit code is 3** (control: a clean run is 0). A runner checking returncode catches
   it unaided. Sentinel had already adopted my framing and told me to push it to OpenBolt, so this
   caught a live error of his too. The residual is narrower: a *human* reads *"no tests ran in
   1.18s"* as benign, and so does code treating "collected 0" as nothing-to-do.
2. **`grep -c asyncio tests/test_seat_rebind.py` → 1 read as a dependency. It was PROSE** — line 8
   of my module docstring describing `asyncio.to_thread`. Had that stood I would have declared my
   own clean evidence contaminated and **withdrawn a correct result**. The false positive pointed
   at over-correction; both directions cost.
3. **My own structural guard matched its own retraction.** It asserted the text
   `registered = False` was absent from the seam; the new docstring *quotes* that assignment in
   order to retract it, so the guard went red **on the fix**. Both guards are now `ast` walks.
4. **I reported Sentinel's `app.py:1835 register()` line as not found in my tree. His line was
   right; my instrument was wrong** — the call is `asyncio.to_thread(self.seat.register)`, a bound
   method reference, which a `register()` grep cannot see. I checked before reporting it as an
   error, which is the only reason it did not become one.
5. **I downgraded my own 991/1000 unprompted** after OpenBolt's `pytest-asyncio` finding landed
   near evidence I had already had accepted. Nobody asked.

⭐ Four of these five are the same root wearing different clothes — **text matched where a
STATEMENT was meant** — and each fooled a different instrument: a guard, a classifier, an audit,
and a search. Finding it once did not inoculate me against the next three.

---

## Verification block — every claim above, as commands

```bash
cd C:/Projects/LiteTUI
git log --oneline -6 main                                   # b82668a .. 71c347b
for s in 04082db b774fc0 77e997a; do git merge-base --is-ancestor $s main && echo "$s ON MAIN"; done
git cat-file -e main:tests/test_seat_rebind.py && echo present
git cat-file -e main:tests/test_mcp_timeout_bounds.py && echo present
git -C .worktrees/seat-mcp status --porcelain | wc -l        # 0
python -m pytest tests/test_seat_rebind.py tests/test_seat_convo_identity.py \
  tests/test_fleet_identity.py tests/test_mcp_timeout_bounds.py -q -p no:asyncio   # 40 passed
```
