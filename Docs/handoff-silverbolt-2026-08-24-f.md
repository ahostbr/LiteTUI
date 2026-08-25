# Handoff — SilverBolt, 2026-08-24 (sixth; T084 + T085 landed, main green)

**Supersedes** `Docs/handoff-silverbolt-2026-08-24-e.md`
(verified present: `git ls-files --error-unmatch Docs/handoff-silverbolt-2026-08-24-e.md`).

On `main`, and **main is GREEN on both instruments** — which matters because a peer is still
holding a branch off it on the belief that it is red. See §4.

---

## 1. IN FLIGHT — NOTHING OF MINE

```bash
git -C C:/Projects/LiteTUI rev-parse --short HEAD          # 0187d4e
git -C C:/Projects/LiteTUI ls-remote origin main | cut -c1-7   # 0187d4e — equal
git -C C:/Projects/LiteTUI status --porcelain | grep -v '^??' | wc -l   # 3, NONE mine
```

The three dirty files are **not mine and were not touched**: `pyproject.toml` + `uv.lock` carry
someone else's `websockets>=15` (chrome-bridge), and `src/litetui/paths.py` is a **CRLF phantom with
an empty content diff** (`git diff --numstat src/litetui/paths.py` prints nothing).

| sha | row |
|---|---|
| `0187d4e` | **T085** scheduled turns always run auto · `scheduled` stops being a selectable mode · the light warning · the settings migration |
| `21b773f` | handoff-e + handoff-d §2 corrected in place + the backstop documented |
| `f8845ac` | **the regression `cc230c8` shipped** — shift+tab ate `prev_question` · `cycle()` unknown → floor |
| `cc230c8` | **T084** the authority setting governs unattended turns · default `autonomous` · footer level · shift+tab cycle |

**GATE on `0187d4e`, both instruments, exits captured off each command on its own line:**

```
pytest -q tests/          1362 passed                                    REAL EXIT 0
python tests/run_all.py   all green — 119 pytest file(s) + 13 script(s)  REAL EXIT 0
```

## 2. 🔴 CAVEATS RIDING THE GREENS — READ BEFORE TRUSTING ANY PASS LINE

- **INSTRUMENT 1 IS *DESIGNED* NOT TO SEE SCRIPT-STYLE TEST FILES.** `tests/conftest.py:131` puts
  every such file into `collect_ignore`, deliberately, because importing one runs its assertions at
  module level and kills the run with an INTERNALERROR.
  ```bash
  python -m pytest --collect-only -q tests/test_ask_user_question.py   # "no tests collected"
  ```
  ⇒ The two instruments have **deliberately disjoint coverage**. This is also, finally, the named
  reason their totals were never comparable. **One green plus one unfinished run is ONE instrument,
  not one and a half** — that is exactly how `cc230c8`'s regression reached main.
- **Order/timing-sensitive tests still exist.** A green means the ordering landed right. Both
  instruments, and for anything touching sidebars or focus, twice.
- **🔴 `$?` AFTER A PIPE MEASURES `tail`.** I reported `RUNALL REAL EXIT: 0` for a run whose own log
  said `FAILED (2)`. Both instruments are now separate statements with the exit captured
  immediately. **The defect entered during a cleanup pass** — both runs were correct until I tidied
  them into one block. Full account: memory `project_exit_code_after_a_pipe.md` (a POINTER; the
  canonical record is in the `C--Projects` store).
- **`tests/test_command_palette.py` cannot run standalone** (13/15 fail alone at HEAD). Unchanged.

## 3. ABSENT BY DECISION — AND THE GATE THAT DEFENDS EACH

- **`Job.tool_profile` NOT deleted.** Vestigial for the third time in one evening; its docstring now
  records all four positions so the git history reads straight. Removing a persisted field is a
  schema change and does not belong inside an authority change.
  Gate: `test_an_old_job_file_carrying_the_dead_key_still_loads`.
- **`side_panel.handle_reverse_tab`'s BACKSTOP is still a hand-kept class list**, demoted rather
  than removed, and **it fails PERMISSIVE if it is ever wrong**. Documented in place with the
  reachability condition (only fires when the focus trap has already failed). Rejected alternatives
  are named there: a marker attribute merely relocates "remember to add it", and registering open
  dialogs changes the dialog lifecycle.
  Gate: `test_a_dialog_keeps_its_own_shift_tab` + `test_ASK_USER_QUESTION_keeps_shift_tab_for_prev_question`.
- **The settings migration was folded into T085 rather than given its own row.** Offered to Sentinel
  to split; no answer yet. Gate: `test_a_stored_scheduled_level_does_not_CRASH_the_settings_screen`.
- **The footer authority field has NO toggle.** T084 happened because the authority in force was
  invisible. Gate: `test_absence_renders_as_ABSENCE` + its unconditional placement.

## 4. OWED — SPLIT BY OWNER

**MINE** — nothing. Every row is committed, pushed, and gated.

**OPENBOLT'S** —
- 🔴 **HE IS WORKING FROM A STALE FACT AND IT IS BLOCKING HIM.** His latest (`aa0ff14c`) says *"Your
  cc230c8 red is the only thing between us and a green main"*. **That red was fixed at `f8845ac`**,
  and main is green at `0187d4e` on both instruments (§1). His `87077b5` is held off main on a
  belief that no longer holds. **Tell him before he waits any longer.**
- The `test_confirm_stop_sidebar` swap residual: **UNREPRODUCED, explicitly NOT fixed** (his word,
  and correct). ~180 runs across isolated / file-ordered / loaded arms, zero reproductions since the
  single sighting. `87077b5` makes the assertion self-diagnosing — `settled=False` ⇒ the carry never
  arrived (LOST), `settled=True` ⇒ it arrived and something moved it after. **Opposite fixes.**
  What he needs from anyone hitting it: **do not discard the output** — the `E ` line is the answer.

**RYAN'S** — nothing outstanding.
✅ **The one item I could not verify is DISCHARGED**: he pressed shift+tab — *"works switches"* — so
the binding is confirmed on his real terminal, and my parser measurement (Textual 8.0.2 turns
`ESC [ Z` into `shift+tab`) held. `temp-working-dir/keyprobe.py` is no longer needed for this.

**STANDING INSTRUCTION, relayed via OpenBolt:** *Ryan told him to stop generating load on his box.*
**I stopped my own load loop before that arrived** (`TaskStop` on the 60-iteration run) and have
started nothing since. Do not start load generators on this machine.

## 5. MY OWN CORRECTIONS AND RETRACTIONS

1. **I SHIPPED A REGRESSION IN `cc230c8`.** My `priority=True` shift+tab binding ate
   `AskUserQuestionBody.prev_question`; `test_ask_user_question.py` went 48/48 → 43/48. Fixed at
   `f8845ac` with a proximity walk.
2. **THE CAUSE WAS THE DEFECT CLASS T084 EXISTS TO REMOVE, WRITTEN INSIDE THE FIX FOR IT.** I derived
   `unattended()`, `stops_you()` and `cycle()` off `PROFILES` — refusing three drift pairs and
   congratulating myself in the docstrings — then wrote a literal **class list** one file away.
   **Deriving in the module you are thinking about does not protect the module you are not.**
3. **AND MY OWN TEST HID IT.** It exercised ConfirmStopBody in a SidePanel — *one* of the two
   surfaces — and passed. A control that validates the **instrument**, not the **scope**.
4. **I DID IT A SECOND TIME THE SAME NIGHT.** I shipped `SELECTABLE_PROFILE_NAMES` as a module
   **constant** and broke three tests. `settings_screen.tool_profile_choices` already carried the
   reason — *"a constant is computed once at import and a test cannot then add a profile and watch
   it appear"* — **in a file I had read, about the exact thing I was doing.**
   ⇒ Both times the rule was written down, in this codebase, next to the code I was editing. I did
   not re-read it **because I was writing the fix rather than looking for the rule.**
   ⭐ The cheapest detector I already had and did not use: `grep -rn '"shift+tab"' src/litetui/`
   returns the complete answer in one line. **I ran it to FIND the hazard and never re-ran it to
   VERIFY MY COVERAGE of it.** My list held one real surface, one entry that does not bind the key
   at all (`_ModalHost` binds only `escape`), and one miss — while reading as deliberate coverage.
5. **I GENERATED A FALSE GREEN WITH MY OWN INSTRUMENT** — `$?` after a pipe. §2.
6. **I MISREAD MY OWN HARNESS'S SCRATCH FILE AS A RESULT.** My capture loop deletes `run$i.txt` on
   pass; I listed the directory mid-run, saw one, and announced I had caught a failure. I had not.
   Retracted within a minute. **A partially-written artefact of your own harness looks exactly like
   a result.**
7. **I NEARLY FILED NOISE AS A REGRESSION.** "1 failure in 6 with my change vs 0 in 6 without" — two
   small samples with a difference read into them. 21 further runs put it at 1-in-21, consistent
   with the known baseline, and the test had already failed at `8484923`, before T084 existed.
8. **A BACKTICKED INLINE INBOX MESSAGE RAN AS COMMAND SUBSTITUTION** and silently ate a clause. The
   CLI's own `--help` warns about exactly this and I had read it that session. Corrected via
   `--body-file` (`d115a97c`). **Reading a warning is not having a habit.**
9. **I FAILED TO NOTICE A SEND WAS REFUSED.** A message to Sentinel was rejected (unregistered id)
   and I only caught it because the *same body* to OpenBolt succeeded on the line below. Transient;
   registry since confirmed healthy, unforced sends work.
10. **MY MUTATION HARNESS TIMED OUT MID-ARM AND LEFT THE CODE MUTATED.** Its final restore never
    ran; `textfmt.py` sat holding the deliberately-broken note. Caught only by running `md5sum -c`
    **after a CRASHED harness** — the run where the instinct is to skip the check. **The abort path
    is precisely the one that skipped the restore.** Filed to
    `project_harness_reverted_code_under_test.md`.
11. **I ARGUED AGAINST RYAN'S `_fire_job` RULING AND WAS OVERRULED**, then his later ruling removed
    the question entirely. The concern is recorded, not re-litigated.

## 6. FOR THE FAR SIDE — FIRST THREE COMMANDS

```bash
git -C C:/Projects/LiteTUI log --oneline -4 origin/main
sed -n '/_fire_job/,/AUTONOMOUS/p' src/litetui/app.py | head -40   # the line that held 3 values tonight
python -m pytest -q tests/ ; python tests/run_all.py               # BOTH — they see different files
```

Nothing to build. **Suggested skills for the next seat:** none required; `/handoff` when you finish.
⚠️ `/sentinel` is the ORCHESTRATOR protocol — **not for this seat.** Ryan flagged that explicitly
(*"DO NOT sentinel save that is a type not meant for you"*) after a mistyped invocation. This seat is
tier **worker**; it reports through the inbox and does not drive fleet lifecycle.
