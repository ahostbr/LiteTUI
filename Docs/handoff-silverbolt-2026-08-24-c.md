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
git rev-parse --short HEAD origin/main          # 16bc4e7 (or later) == same
```

| sha | what |
|---|---|
| `d9a361f` | merge T062 (docs only, 1 file, 0 code) |
| `fb49b9b` | merge T072 — tools-off refuse. **Merged BEFORE the decomposition, deliberately.** |
| `cf517fe` | merge T070 — the whole app.py decomposition, 70 commits |
| `22349d8` | fix `/keys` toggle |
| `80162a7` | T073 engine half — rule_key + evaluate(always_allow=, deny=) |
| `c55d9d2` | **T073 items 1–3 — tri-state modal, settings, call site. WIRED END TO END.** |
| `16bc4e7` | **T073 items 5–6 — deny stops the turn; every refusal in prompts/tool-denied.md** |

Board: T062 · T066 · T070 · T072 **done**. T073 **building**, mine.
⚠️ §2 items 1–3 AND 5–6 are now DONE — see §10, which supersedes them. Only item 4 is left.

## 2. T073 — WHAT LANDED, WHAT HAS NOT

**LANDED at `80162a7`, engine only, wired to nothing:**
- `tool_policy.rule_key(tool_name, capabilities)` — the standing-rule identity.
- `evaluate(..., *, tool_name="", always_allow=frozenset(), deny=frozenset())`.
- `tests/test_tool_standing_rules.py`, 6 tests.

**Items 1–3 below LANDED at `c55d9d2`** — the text is kept for its reasoning, but the STATE
sentences are corrected in place rather than banner-corrected, because a banner does not reach a
claim a reader lands on directly. Items 4–6 are still owed. Full detail in §10.

1. ✅ **DONE — the modal is `ModalScreen[ToolApproval]`** with three buttons (deny / once / always).
   It is NOT a string enum, and §10 explains why that would have inverted the guard.
2. ✅ **DONE — settings persistence.** `tool_always_allow` / `tool_deny` exist as `list[str]` —
   `list[str]` because `_coerce` (settings.py) handles that type and **not** `frozenset`; converted
   at the edge in `app.py`. ⚠️ Adding them ALSO required rows in `settings_screen.py` — see §10.
3. ✅ **DONE — the call site** now passes `tool_name=name` and both rule sets, and writes the rule
   via `_remember_tool_rule` when the human answers "always".
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

**MINE** — items **4–6** of §2 (1–3 landed at `c55d9d2`). Sentinel's ordering was **always-allow
FIRST**, because it is the one that makes the rest usable — that is discharged. Field evidence for
why it came first: Ryan killed his own qwen seat rather than keep answering the modal, so the guard
was not overridden, it was ROUTED AROUND.

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
git -C C:/Projects/LiteTUI log --oneline -3 origin/main     # c55d9d2 at the top
grep -c "^def rule_key" src/litetui/tool_policy.py          # 1 = the engine is there
grep -n "ModalScreen" src/litetui/tool_approval.py          # [ToolApproval] = items 1-3 LANDED
```

🔴 **THIS SECTION IS SUPERSEDED BY §10.** It used to say "then §2 item 1: the tri-state modal" —
**that is DONE** (`c55d9d2`). If the third command still prints `ModalScreen[bool]` you are not on
`c55d9d2`; check the ref before building anything. The remaining work is §2 items **4, 5, 6**, and
two of the three are blocked on a Ryan ruling — read §10 first.

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

## 10. ITEMS 1–3 ARE DONE (`c55d9d2`). WHAT IS ACTUALLY LEFT, AND WHAT BLOCKS IT

**Supersedes §2 items 1–3 and §8's "then item 1".** Always-allow now works end to end: the modal has
three answers, settings hold the rule sets, the call site passes them and writes the rule.

### The one design fact worth carrying forward

The modal returns a frozen **`ToolApproval`** (`approved`, `remember`) whose `__bool__` is
`approved` — NOT a string enum. That is deliberate and it is a trap worth stating out loud:

> Three string states (`"deny"` / `"once"` / `"always"`) read perfectly and are silently
> catastrophic, because **`"deny"` is TRUTHY**. `if not approved:` would stop firing and the Deny
> button would begin RUNNING the tool — while every test that exercises the allow path still passes.

`DENIED` is falsy, both approvals are truthy, and `push_screen_wait`'s None-on-teardown is falsy
too, so every truthiness check written against the old `bool` keeps working AND keeps failing
closed. **`test_denial_is_falsy_and_both_approvals_are_truthy` is the only thing standing between
this file and that refactor. Do not delete it as redundant — it looks redundant.**

### STILL OWED — and two of three need Ryan, not code

| # | item | state |
|---|---|---|
| 4 | `AUTONOMOUS` profile — third `ToolProfile`, all in `allow` (tool_policy.py:50, :127) | **BLOCKED ON A FILE BOUNDARY** — see below |
| 5 | deny-stops-turn at the call site | ✅ **DONE `16bc4e7`** — Ryan ruled REPLACE |
| 6 | every refusal → `prompts/tool-denied.md` | ✅ **DONE `16bc4e7`** — Ryan ruled migrate ALL FIVE, `TOOLS_DISABLED_RESULT` included |

**ITEM 4 IS NOT MERELY DEFERRED ANY MORE — IT CANNOT BE DONE ALONE.**
`TOOL_PROFILE_CHOICES` is **hardcoded at `settings_screen.py:77–80`**, and that file is shared with
OpenBolt's T075 under Sentinel's boundary (message `77153e6c`). Adding a `ToolProfile` without the
matching row there ships a profile no one can select; adding the row without the profile fails the
`tool_policy_profile not in PROFILE_NAMES` check at save. **It is one edit across two files, one of
which is not mine to take.** Ask Sentinel to sequence it.
📌 That hardcoded list is itself the hazard: the UI's set of profiles and `tool_policy.PROFILES` can
drift silently in one direction. Deriving the choices from `PROFILE_NAMES` would remove the whole
class — and would also make item 4 a one-file change.

### WHAT ITEMS 5 AND 6 ACTUALLY CHANGED

- **Deny ends the turn.** The refusal is still returned and still recorded; the loop just gets no
  further round-trip. `_stop_reason` names the cause, and both it and `_stop_requested` are cleared
  at turn start so a reason cannot outlive its turn.
- 🔴 **A LIE WAS ALREADY REACHABLE AND IS NOW FIXED.** `_stream`'s tool loop breaks at the top on
  `_stop_requested` and **falls through to the bottom of the function**, which printed
  *"reached N tool iterations — raise it in /settings"*. Any early break blamed a cap that was never
  hit. Reachable before this change by pressing **Escape while a TOOL is executing** rather than
  while the model streams. **Nothing in the suite mentioned that message**, which is why it lived.
- **Refusals live in `prompts/tool-denied.md`.** `validate_tool_denied()` RAISES on a broken file —
  loud, at test time. `tool_denied()` never raises for the same faults and falls back to a built-in
  constant, because the moment a refusal is needed is the worst moment to throw.
  ⚠️ **That is a change of position from §10's earlier wording** ("a missing placeholder must
  RAISE"). Raising at runtime turns a refusal into an exception at the worst possible moment; the
  intent — never render a literal `{name}`, never go silent — is better served by rejecting the
  broken section. Recorded as a reversal, not left to look like the original plan.
- **`TOOLS_DISABLED_PROMPT` did NOT move** and is flagged in place: it is a system-prompt section
  composed at turn start, not a refusal returned in place of a tool result.

Nothing writes `tool_deny` yet — it is `settings.json` by hand. A fourth "Always refuse" button is
the obvious next increment and was **not** built, because "deny forever" from a mid-turn modal is a
bigger commitment than "allow forever" and should be Ryan's call, not a symmetry argument.

### 🔴 THE HAZARD THAT COST RYAN A LIVE ERROR — READ BEFORE TOUCHING `Settings`

**Adding a field to the `Settings` dataclass with no settings-screen control does not hide one
control. It makes Save raise from EVERY TAB.** `settings_screen.py::_collect` walks every field and
refuses the whole write if any widget is missing. Ryan booted inside the 48 seconds between my two
edits, went to **Compaction** — nothing to do with T073 — and got
*"no control found for: tool_always_allow, tool_deny — refusing to save a partial settings object"*.

⇒ **Add the field and its control in ONE edit.** Gate:
`tests/test_settings.py::test_every_field_is_reachable_without_opening_its_tab`. A genuinely
control-less field goes in the deliberate skip tuple in BOTH `_collect` and that test — not nowhere.
✅ The guard is correct and is **not** to be softened: a save that silently drops unknown fields is
how a settings file loses data. It is what made this loud instead of destructive.

### The rival hypothesis, and how it was killed without running anything

Sentinel proposed Textual `TabPane` children mounting **lazily** — `query_one` missing a control
that exists, misfiring for any setting in an unvisited tab. Same symptom, real latent bug if true.

Refuted by a test that was **already green**: at HEAD, before my fields, that test reports **zero**
fields unreachable from an inactive tab. Lazy mounting does not discriminate by which field is
newest — it would have been failing for every field in Compaction, Themes and Interface,
continuously. It failed for two names and they were mine.

> **A hypothesis that predicts a GLOBAL failure is refuted by any standing green test that would
> have caught it. The negative control has been running the whole time.**

⚠️ Path stated: that is Textual's `run_test` headless driver, not a real terminal — the one variable
NOT measured. Corroborating but not decisive: `textual.lazy` / `Lazy(` → **0 hits** in `src/`, and
`settings_screen.py:607` already carries *"If TabbedContent ever mounts panes lazily, this is the
line that catches it."* The live probe (restart → Settings → Compaction → Save) discharges it and
costs one restart; it was not run because Ryan was mid-200k-context run.

### 📌 RYAN RUNS LITETUI FROM THIS SOURCE TREE WHILE YOU EDIT IT

An already-running Python process is unaffected by an edit — imports happen once — so the whole risk
is **when he restarts**. A multi-file change has an intermediate state, and the gap between your
first and last write is a window in which a boot gets a self-inconsistent app. Mine was 48 seconds
and it was enough. **Announce before writing under him; keep multi-file edits to one burst.**

### Gate on `c55d9d2`

`tests/run_all.py` **REAL EXIT 0** — 101 pytest-style files + 13 scripts, 101 predicted before the
run. Direct `pytest -q` over all 114 globbed files: **1174 passed, 0 failed**. The totals differ by
5 because run_all EXECUTES the script-style files rather than collecting them — two scopes, not a
discrepancy.

**Baseline measured, not assumed:** this change produced 7 failures; stashing only my four files and
re-running them at HEAD gave 9 passed, so none were pre-existing. Two of the seven were test doubles
returning a bare `True` from `push_screen_wait`. **The doubles were fixed, not the production code**
— `getattr(answer, "remember", False)` was rejected because it would silently degrade "always" to
"once" if the type ever drifted, turning a loud `AttributeError` into a feature that quietly stops.
