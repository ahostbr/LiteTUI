# OpenBolt — LiteTUI — 2026-08-23 (second session, harness-disabled path)

**Supersedes** `handoff-openbolt-2026-08-23.md` — verified PRESENT on main, 9548 bytes
(`git cat-file -e main:Docs/handoff-openbolt-2026-08-23.md`). Its §2a "the autoscroll flake" is
now **DISCHARGED** and that document is stale on that point only.

**Seat:** OpenBolt (`431b1349-7135-4ba6-9998-f6a215fea839`), worker.
**Branch:** `chore/package-move` @ `cfaaf86` — **0 ahead of main, 22 behind, porcelain 0.**
Verify: `git rev-list --count main..HEAD` → 0 · `git status --porcelain | wc -l` → 0.
⚠️ **THIS FILE IS BRANCH-ONLY UNTIL SOMEONE MERGES IT.** A sweep of `main` will not see it.

**What landed, all merged into `main` via `942e27c`:**

| sha | what |
|---|---|
| `f32fa33` | a disabled seat must not announce itself into the chat log |
| `cfaaf86` | one door for the disabled path — poll/send/discover reached the live fleet |

Verify each: `git merge-base --is-ancestor <sha> main`. Reasoning is in the commit bodies; it is
not repeated here.

---

## 1. IN FLIGHT

**NOTHING.** No source file modified anywhere, no uncommitted analysis, no probe half-run.
`git status --porcelain` → 0 in this worktree.

---

## 2. OWED, SPLIT BY OWNER

### 2a. MINE
**Nothing.** Stated explicitly so the empty bucket is not read as an omission.

### 2b. THEIRS

- **ANVIL — the node-level dispute, and he asked that I not re-run it.** My sub-measurement
  `T3 alone (single node id) 0/20` on the broken tree did not reproduce: his control failed at
  iteration 4 on `verify/jobobject`. **The counts do not actually conflict** — rule of three puts
  the 95% upper bound of 0/20 at 15%, and his 1/20 point-estimates 5%. What was wrong was my
  *prediction* from it (see §5). Having the author re-derive a disputed number is how a measurement
  gets talked into agreeing with itself; it is his.
- **ANVIL — the binary-arm instrument is handed over as an artifact, not a description:**
  `C:\Users\Ryan\AppData\Local\Temp\litetui-binary-arm\binary_arm.py`, 3694 bytes,
  sha256[:16] `4abfcd9f9bad5a42`. Usage `binary_arm.py <REPO_ROOT>`; exit 1 = MOUNTED (unfixed),
  0 = ABSENT (fixed). Verified both directions on **his** tree before handover:
  `.worktrees/verify` → children 2, MOUNTED, exit 1 · `.worktrees/runtime` → children 1, ABSENT,
  exit 0.
- **SENTINEL** — this handoff needs merging or cherry-picking; it is branch-only (above).

### 2c. RYAN'S

- **Every push.** Five repos have remotes and three are ahead — query, not a value:
  `git -C <repo> ls-remote origin refs/heads/<branch>` vs local HEAD. **Use `ls-remote`, never
  `rev-list`** — `rev-list` answers from a local cache and read 4-unpushed for a repo with nothing
  unpushed tonight.
- **126 uncommitted paths inside the nested repos** — covered by neither the snapshot nor the
  bundles. Re-derive: `git -C <repo> status --porcelain | wc -l` per repo. Includes
  `from-cogs-to-cosmos` 27 (his Minecraft pack work) and `LiteImage` 20 (0 ahead **and** has a
  remote, so it passes every criterion applied so far).
- **Whether the four no-remote repos get remotes.** `SimCraft` (1,559 commits),
  `from-cogs-to-cosmos` (40), `drmario` (24), `orch-e2e-site` (21). Committed history is bundled
  and verified; adding a remote is a bigger decision than a push and is his alone.

---

## 3. ABSENT BY DECISION

| Not done | The gate that defends it |
|---|---|
| **The narrow "gate only when NEW is the real maildir" variant** | Sentinel's ruling, and his reason is better than mine: a path comparison is safety that depends on a fact about the *environment* — the same shape as the three accidents — and "redirected but still live" fails it **invisibly**. My blunt guard + precondition-checked opt-out stands. |
| **`test_tool_cancel.py` untouched** | Measured, not assumed: interleaved 8 rounds, BEFORE 4/8 vs AFTER 6/8, Fisher p ≈ 0.6 — no evidence my change affects it. It is flaky at HEAD independently (2/6 with my change absent). Anvil owns it. |
| **No sleep, no retry, no widened tolerance in the flake fix** | Sentinel's constraint, and `9d8abfe`'s own docstring independently rejects the same shortcut. |
| **`.worktrees/verify` NOT removed** | Sentinel's standing order: it is the only pre-fix tree and is irreproducible once dropped. Also covered by the workspace rule against removing worktrees without a junction check first. |
| **No remotes added, nothing committed inside the 16 nested repos** | "Commit everything" was issued at the `C:/Projects` level and `git add` on a nested repo makes a gitlink, not files. Acting inside 16 repos is past what that authorised, and some `from-cogs-to-cosmos` files are deliberately uncommitted. |
| **No test deleted or weakened** | The keep-and-fix ruling, now vindicated: every failure of `test_autoscroll_setting_still_gates_the_stream` was reporting a genuine unwanted scroll. Deleting it would have removed the only detector for a live product bug. |

---

## 4. CAVEATS RIDING THE PASS LINES

🔴 **MY `0/20 AFTER` IS FILE-LEVEL, NOT SUITE-LEVEL, AND MUST NOT BE QUOTED AS SUITE-LEVEL.**
Every flake number I produced ran `pytest tests/test_thinking_autoscroll.py -q` — the whole file,
three tests, one process. Evidence it is not a suite run: those runs took 7.3–11.0 s; a full suite
takes 275 s.

⚠️ **THERE IS NO FULL-SUITE BEFORE, AND NOBODY SHOULD MANUFACTURE ONE.** I never measured these
three tests inside a full suite on the unfixed tree. The single pre-fix full-suite run I hold shows
autoscroll green — one sample at ~78–80% pass odds per test, so it says nothing. **UNPROVEN**;
discharged only by running the *file* (never the node) inside a full suite, interleaved, on a
pre-fix tree.

⚠️ **THE INTERLEAVED FLAKE RESULT IS UNDERPOWERED ALONE.** BEFORE 2/20 vs AFTER 0/20 is
Fisher p ≈ 0.24. It is corroboration, not the proof. **The load-immune binary arm is the headline**
(§2b): with the guard armed, HEAD mounts the message and the fixed tree does not. Load changes
*when* a worker lands, never *whether* a branch runs.

⚠️ **FLAKE RATES ON THIS BOX ARE NOT IID — THEY TRACK LOAD.** Replicated independently on
`test_tool_cancel`: a single 60 s threshold classifies 15 of 16 runs. **A FAILURE is valid under
any load; a PASS is only trustworthy under known load.** Any before/after must interleave its arms,
never run them as time-separated blocks.

⚠️ **`n=20` ON THE SINGLE NODE `test_autoscroll_setting_still_gates_the_stream` CANNOT DISCRIMINATE.**
At a ~5% rate it returns 20/20 green **on a broken tree 36% of the time**; n ≥ 59 is needed to push
that under 5%. Run the FILE, not the node. A narrower target is not a stronger test.

📌 **The `arm 5` tests each delete their own accidental protection** — transport stubbed so the CLI
always answers, `registered` forced True, `poll` called directly rather than through the worker.
A test that passes for the venv's reasons has tested the venv. Every arm that can touch the maildir
is redirected to `tmp_path`, and the opt-outs **assert the redirect before un-gating**
(mutation-checked: `NEW=live` → AssertionError).

---

## 5. MY OWN CORRECTIONS AND RETRACTIONS

**Three hypotheses for the flake died, each measured, each moving 4/20 → 5/20**: layout race
(refuted by its own prediction — `grew=9` on every failure), stale follow anchor (a real defect,
inert here), late unconditional mount scroll (right shape, wrong scheduler). **The flake was real
while all three of my mechanisms for it were wrong.** None is half-applied; the tree holds one
guard.

**I predicted from a pass-count.** I wrote *"he will measure zero"* off `T3 alone 0/20`. That is a
forward-looking property claim and is not derivable from a count — 0/20 bounds p at ~15%, it does
not establish 0. Sentinel filed it as a relay defect; **the sentence was mine.** *12 is not a proof
of never.*

**My census undercounted by 5.** I used `rev-list --count HEAD`, which answers *"how long is this
branch"*; the backup question is *"how much history exists here"* and only `--all` answers it.
`drmario` carries 5 commits on non-default refs. **1,639 → 1,644.** It survived because three of
the four repos agreed — a method right most of the time produces a number nobody re-derives.

**I re-derived a six-week-old documented fact.** The maildir prune is recorded in
`liteharness-maildir-truth-and-type-mismatch` (2026-08-09). I investigated instead of running
`find_conversation.py --search "maildir prune" --mode memory` first. *Why it survived is the
keeper:* two memories were each correct and **neither pointed at the other** — one recorded "done/
prunes", the other prescribed "prove delivery by finding the id". The defect lives only in their
combination, so it appears in neither file. Both now cross-link.

**My hedge was in the wrong sentence.** I wrote "8 processes are STILL ALIVE" and put "some may be
mine" in the *next* sentence. The number travelled; the qualifier did not, and came back as an
instruction to clean up. Truth: 6 `CANCELPROBE` alive, **0 orphaned** — all children of a live
pytest. Acting on it would have killed a colleague's in-flight measurement, **and the corruption
would have presented as more flakiness**, the exact signal being measured.

**My own probe would have handed Anvil a false green.** It hardcoded my worktree, so run from
anywhere it imported the *fixed* tree and printed ABSENT regardless of the tree under test. The
handed-over version takes `REPO_ROOT` as a **required argument with no default** and prints
`litetui.__file__` so the output names the tree it loaded.

**A piped gate reported the pipe's exit.** My first exit-code check printed `exit=0` for both arms;
that was `grep`'s exit, not the script's. Re-checked unpiped: 1 and 0.

---

## 6. SUGGESTED SKILLS FOR THE NEXT SESSION

- **`/arch` and `/library`** — mandatory at session start per `C:/Projects/CLAUDE.md`.
- **`/liteharness`** — register the seat and start the inbox Monitor before any work.
- **`/ls-conversation-lookup`** — and use `find_conversation.py --search "<q>" --mode all`
  **before** investigating anything that smells like infrastructure behaviour. Tonight that one
  call would have replaced an entire investigation.
