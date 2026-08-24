# Handoff — SilverBolt, 2026-08-24 (third; T073 engine landed)

**Supersedes** `Docs/handoff-silverbolt-2026-08-24-b.md`, which merged to `main` with T072 at `fb49b9b`.
Verify: `git cat-file -e origin/main:Docs/handoff-silverbolt-2026-08-24-b.md`

This one is **on `main`**, not a side branch.

---

## 1. IN FLIGHT — NOTHING. EVERYTHING OF MINE IS COMMITTED AND PUSHED.

```bash
git -C C:/Projects/LiteTUI status --porcelain   # 5 lines, NONE of them mine:
#   M prompts/systemprompt.md   <- RYAN'S, and FENCED (PLAN.md:1024). Do not touch, do not commit.
#   ?? .playwright-cli/  ?? skills/find-claude-skills/  ?? temp/  ?? tools/pccontrol/screenshot.py
git rev-parse --short HEAD origin/main          # 80162a7 == 80162a7
```

| sha | what |
|---|---|
| `d9a361f` | merge T062 (docs only, 1 file, 0 code) |
| `fb49b9b` | merge T072 — tools-off refuse. **Merged BEFORE the decomposition, deliberately.** |
| `cf517fe` | merge T070 — the whole app.py decomposition, 70 commits |
| `22349d8` | fix `/keys` toggle |
| `80162a7` | **T073 engine half — rule_key + evaluate(always_allow=, deny=)** |

Board: T062 · T066 · T070 · T072 **done**. T073 **building**, mine.

## 2. T073 — WHAT LANDED, WHAT HAS NOT

**LANDED at `80162a7`, engine only, wired to nothing:**
- `tool_policy.rule_key(tool_name, capabilities)` — the standing-rule identity.
- `evaluate(..., *, tool_name="", always_allow=frozenset(), deny=frozenset())`.
- `tests/test_tool_standing_rules.py`, 6 tests.

**NOT STARTED — the other three quarters of the row:**

1. **The modal is still `ModalScreen[bool]`** with two buttons. Needs a tri-state
   (deny / once / always) and a third button. `src/litetui/tool_approval.py`, 87 lines.
2. **Settings persistence.** No field exists yet. Proposed `tool_always_allow: list[str]` and
   `tool_deny: list[str]` — `list[str]` because `_coerce` (settings.py:302) already handles that
   type and **not** `frozenset`. Convert at the edge, in `app.py`.
3. **The call site**, `app.py:1403` — `evaluate()` is not yet passed `tool_name=name` or the rule
   sets, which is exactly why nothing has changed behaviour yet.
4. **`AUTONOMOUS` profile** — a third `ToolProfile`, everything in `allow`, nothing in `confirm`.
   `PROFILE_NAMES` at tool_policy.py:50, `PROFILES` at :127.
5. **Deny-stops-turn** — `app.py:1416` returns `[policy denied by user] {name}, False` and the loop
   CONTINUES. Ryan wants it to stop the turn. **Implement as a CHOICE, not a deletion:** my own
   `action_cancel_tool` docstring already ruled the other way on purpose (the turn carries on with
   an honest result; Esc stops the whole turn — two deliberate verbs).
6. **Sentinel's item B** — move the four hardcoded refusal strings to `prompts/tool-denied.md`.
   They INTERPOLATE (`{name}`, `{decision.reason}`), so it is a TEMPLATE: a missing placeholder must
   RAISE, and a missing FILE must fall back to a built-in constant, never to silence. A refusal that
   renders its own braces is worse than the hardcoded string it replaced.

### THE SIGNATURE QUESTION, ANSWERED — do not re-derive it

`evaluate()` has exactly **three** call sites: `app.py:1403`, plus helpers in
`tests/test_tool_policy.py:29` and `tests/test_tool_approval.py:12`. Both tests wrap it, so
**keyword-only params with defaults are backward compatible** — 16 existing policy tests passed
untouched. `tool_name` defaults to empty and **an empty name can never match a rule**, which is the
fail-safe direction.

### THE TWO ORDERINGS THAT ARE THE SAFETY — do not "simplify" them

- **Keyed by tool AND capabilities.** `SHELL_POLICY` is `process_execution` normally and ALSO
  `destructive_irreversible` when args match the destructive pattern. A name-only key takes one
  approval of "run a command" and silently grants a later authority the human never saw.
- **An allow rule turns CONFIRM into ALLOW, never DENY into ALLOW.** Otherwise "always" in an
  interactive modal widens the unattended `scheduled` profile. **DENY WINS** over everything.

## 3. THE MUTATION CONTROL — ALL FOUR RUN, UNION KILLS ALL SIX TESTS

| mutation | edit | kills |
|---|---|---|
| `namekey` | `rule_key` returns the name only | scoping + **escalation guard** |
| `early` | consult `always_allow` BEFORE the profile denial | confirm-silencing + **scheduled widening** |
| `nodeny` | disable the deny gate | **deny-wins** |
| `noname` | drop the `tool_name` guard on the allow gate | **nameless-call guard** |

No test in that file is decorative. The mutation script lived in the scratchpad and is
session-scoped — **rewrite it, do not hunt for it.**

## 4. 🔴 MY LOOP DELETED THE CODE UNDER TEST. DO NOT REBUILD IT THAT WAY.

My first mutation loop ran `git checkout --` on `tool_policy.py` between cases to restore the
source — and the implementation was **UNCOMMITTED**, so the restore **deleted** it. The file then
read *clean*, and **clean means HEAD, not correct.** The next two mutations silently measured the
ORIGINAL code: one produced an `ImportError` I nearly read as a test problem, the other reported
`anchor count 0`.

⇒ **COMMIT THE CODE UNDER TEST BEFORE MUTATING IT.** Then `git checkout --` restores your work
instead of reverting it. That is why `80162a7` was pushed before the last three mutations ran.
📌 This is my own filed law (`project_harness_reverted_code_under_test`) walked into directly.

## 5. OWED — SPLIT BY OWNER

**MINE** — items 1–6 of §2. Sentinel's ordering: **always-allow FIRST**, because it is the one that
makes the rest usable. Field evidence: Ryan killed his own qwen seat rather than keep answering the
modal, so the guard was not overridden, it was ROUTED AROUND.

**RYAN'S** — (a) the `prompts/systemprompt.md` edit in the working tree is his and uncommitted;
(b) ruling on whether `TOOLS_DISABLED_*` joins the refusal-string migration (propose, do not
assume); (c) whether deny-stops-turn replaces or supplements the existing verb.

**SENTINEL'S** — the `liteharness-oss/hooks.py` parking-guidance edit is still **uncommitted in the
working tree**, and that tree is production for the per-turn nudge: an uncommitted edit there is
live before it is reviewed.

## 6. CAVEAT RIDING A PASS LINE

T072's live arm passed **through a direct client, not the TUI**. The model half is measured — round
1 emitted a structured call with 0 chars of content, round 2 ended with `finish_reason stop` and a
plain-language refusal. The TUI's *rendering* of that refusal is only unit-covered. Sentinel
accepted this and declined a separate ceremony; it gets exercised by the next row that drives the TUI.

## 7. MY OWN CORRECTIONS THIS SESSION

1. **`git commit -m` with backticks ate a sentence** — command substitution. I had a memory file
   about it filed against `liteharness send` and did not transfer it, because I had learned it as a
   fact about a TOOL. Then I over-corrected to "the shell is the mechanism" — **also wrong**: a
   QUOTED HEREDOC is the shell and is immune. **The quoting context is the mechanism.** Attach a
   rule to the narrowest thing that carries the fault; too broad forbids safe practice and *feels*
   more rigorous, which is why nobody challenges it.
2. **"1 behind"** — I told OpenBolt his branch was 1 behind main. It was **12**. No command outputs
   1; I reasoned it. The verified parts of that message had commands; the wrong part had a sentence.
3. **":556 is not live for Ryan"** — false, and false in the dangerous direction: it made a live
   site look like someone else's problem. I had already printed the three wired hooks that
   disproved it and reasoned past them.
4. **"the preamble is stuck until restart"** — it refreshes at any registration, and `register` is
   wired to SessionStart, PreCompact AND PostCompact. Both my propagation claims ran toward "worse
   than it is", which is the direction that gets less scrutiny because it sounds like caution.
5. **"strangers means not in the corpus"** — falsified by a measured counterexample; ranking is
   term rarity, not membership.
6. **An imported example I never verified** — I copied a peer's two-query demonstration into my
   memory entry and one of the two strings is not in that file at all. **A correction can import a
   fresh error from the material it summarises.**
7. **The board-integrity gate** produced ~300 false positives (fleet-wide board, one repo grepped),
   then 2 more after narrowing (sub-task work committed under the parent id; T066's work living in
   LiteSuite).
8. **An instrument overran a section boundary** and failed a correct edit. A window too wide
   fabricates; one too narrow conceals. Both present as a confident number.

## 8. FOR THE FAR SIDE — FIRST THREE COMMANDS

```bash
git -C C:/Projects/LiteTUI log --oneline -3 origin/main     # 80162a7 at the top
grep -c "^def rule_key" src/litetui/tool_policy.py          # 1 = the engine is there
grep -n "ModalScreen" src/litetui/tool_approval.py          # still [bool] = item 1 is owed
```

Then §2 item 1: the tri-state modal. Not the autonomous profile, not the prompt migration.

## 9. APPENDED AFTER THE FIRST PUSH — TWO SHELL HAZARDS YOU WILL HIT

**Filed since: `e659381` on `ahostbr/dotclaude`** —
`project_state_the_path_beside_the_repro.md`, 37 memory files / 37 linked / 0 orphans.

**HAZARD 1 — `subprocess.run(["bash","-c", cmd])` DOES NOT PRESERVE `<<'QUOTED'` ON THIS BOX.**
Backticks inside the heredoc body EXECUTE. The Bash tool's own heredoc DOES preserve them.
Verified both directions. **You will hit this**, because every mutation script in section 3 works
by spawning bash from Python. Two surfaces that look identical in a transcript, opposite behaviour.

**HAZARD 2 — AN OPEN, UNEXPLAINED SHELL DEFECT. Do not close it with a theory.**
*"unexpected EOF while looking for matching quote"* from the Bash tool on multi-line payloads,
reproduced independently by two seats. **It is NOT a heredoc defect** — one instance had no heredoc
at all — and both of us mis-framed it as one for an hour.
Eliminated ON THE FAILING SURFACE: content · redirect · `cd &&` · command shape · backticks ·
apostrophe parity (1/2/3) · em dash · heredoc-presence · scale. NO THEORY. STILL OPEN.
⇒ Workaround that always works: write the file with a direct file write, not through the shell.

⭐ **AND THE REASON THIS SECTION EXISTS AT ALL:** I bisected that failure to a 3-line minimal case
with a character-level trigger and nearly published it as solved. It was measuring a DIFFERENT
EXECUTION PATH. **A reproducer that reproduces *a* failure is not a reproducer of *the* failure —
state the path beside the repro.** The bisect and the narrowing are exactly what would have made it
land unchallenged; rigour on an unattributed measurement buys confidence, not correctness.
