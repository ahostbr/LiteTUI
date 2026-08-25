# Handoff — SilverBolt, 2026-08-24 (fifth; T084 landed, one item owed by RYAN)

**Supersedes** `Docs/handoff-silverbolt-2026-08-24-d.md`.
Verify: `git cat-file -e origin/main:Docs/handoff-silverbolt-2026-08-24-d.md`

On `main`. T084 is the whole session since the last handoff. **The one open item is not code — it
is a keypress only Ryan can make.**

**GATE — BOTH INSTRUMENTS, BOTH GREEN, on `f8845ac`:**

```
pytest -q tests/       1355 passed              REAL EXIT 0
python tests/run_all.py  all green — 119 pytest file(s) + 13 script(s)   REAL EXIT 0
```

Exits captured off each command on its own line, never off a pipe — see §3.

---

## 1. IN FLIGHT — NOTHING. ONE ACCEPTANCE CRITERION IS OWED BY RYAN.

```bash
git -C C:/Projects/LiteTUI status --porcelain | grep -v '^??'   # pyproject/uv.lock are NOT mine
git rev-parse --short HEAD; git ls-remote origin main | cut -c1-7   # equal
```

| sha | row |
|---|---|
| `cc230c8` | **T084** the authority setting governs unattended turns · default `autonomous` · footer level · shift+tab cycle |
| `f8845ac` | **the regression cc230c8 shipped** — shift+tab ate `prev_question`; proximity walk, not a class list · `cycle()` unknown → floor |
| `this commit` | this file + the §2 correction to handoff-d |

## 2. 🔴 THE ONE THING THAT IS NOT VERIFIED, AND IT IS THE HEADLINE FEATURE

**I cannot press a key.** shift+tab is bound, tested, and mutation-gated, but every test injects the
key **by name** through Textual's pilot. What I could measure:

> Textual 8.0.2's `XTermParser` turns the legacy back-tab sequence `ESC [ Z` into the key name
> `shift+tab`. So IF the terminal emits back-tab, the binding fires.

What I could **not** measure: whether **Ryan's Windows Terminal** emits `ESC [ Z`. This box has
history here — `ctrl+enter` and `ctrl+shift+enter` BOTH arrive as `ctrl+j` (app.py's own comment,
measured 2026-08-21). The mechanism that makes shift+tab different is that **back-tab has a
pre-kitty encoding and ctrl+enter has none** — which is a reason to expect it to work, not evidence
that it does.

⇒ **ACCEPTANCE: Ryan presses shift+tab once and the footer level changes.** Sentinel has this and is
putting it in front of him. Until then the feature is UNPROVEN ON THE REAL TERMINAL and **every test
passes either way** — which is exactly the shape that makes it worth writing down rather than
filing as a caveat.
Probe if it fails: `temp-working-dir/keyprobe.py` (needs a human to press keys; writes key names to
`keyprobe.log`).

## 2b. 🔴 cc230c8 SHIPPED A REGRESSION. `f8845ac` FIXES IT. READ THIS BEFORE TRUSTING A GREEN.

`AskUserQuestionBody` binds shift+tab to **`prev_question`**, not to reverse focus. T084's
`priority=True` app binding ate it: `tests/test_ask_user_question.py` 48/48 → **43/48**, first
failure literally *"shift+tab back to q2"*, the other four cascading from the wrong question.

⭐ **HOW IT REACHED `main`, AND THE MECHANISM IS REUSABLE:**
`tests/test_ask_user_question.py` is **script-style**, and `tests/conftest.py:131` puts every such
file in **`collect_ignore`** — deliberately, because importing one runs its assertions at module
level and kills the run with an INTERNALERROR. Verified here:
`pytest --collect-only -q tests/test_ask_user_question.py` → *"no tests collected"*.
**So INSTRUMENT 1 (`pytest -q tests/`, 1353 passed, exit 0) is DESIGNED not to see that file.** The
two instruments have deliberately DISJOINT coverage — this is not a timing accident, and it is why
their totals were never comparable either. Instrument 2 catches it, and it was **still running** when
cc230c8 was committed, on an explicit instruction to land untidied ahead of an inbound agent on
the same files. That trade was right — the collision would have cost more — but:

> **One instrument green plus one instrument unfinished is not one-and-a-half instruments. It is
> one.**

🔴 **THE CAUSE WAS THE DEFECT CLASS T084 EXISTS TO REMOVE, WRITTEN INSIDE THE FIX FOR IT.**
`handle_reverse_tab` enumerated dialog CLASSES (SidePanel, _ModalHost) — a hand-kept list that has
to agree with "every surface binding shift+tab". In the same commit I derived `unattended()`,
`stops_you()` and `cycle()` off `PROFILES` specifically to avoid that, and then wrote a literal
class list one file away. **Deriving in the module you are thinking about does not protect the
module you are not.**

⭐ **AND MY OWN TEST HID IT:** `test_a_dialog_keeps_its_own_shift_tab` used ConfirmStopBody in a
SidePanel — one of the two surfaces — and passed. A control that validates the **instrument**, not
the **scope**. Now a proximity walk (first ancestor that binds the key itself, skipping `Screen`),
so a third surface needs no entry anywhere. Gate: **arm K** — disable the walk and only the NEW
test goes red while the SidePanel one stays green, reproducing the blind spot exactly.

## 3. 🔴 CAVEATS RIDING GREENS

- **A green here does not close an order-sensitive test.** Unchanged from handoff-d §3. Both
  instruments, twice, for anything touching sidebars or focus.
- **`run_all.py` and `pytest -q tests/` ARE DIFFERENT INSTRUMENTS** with different denominators
  (run_all adds 13 script-style files that pytest cannot collect — `test_footer.py` calls
  `sys.exit()` at module level). **Never compare the two totals.**
- **🔴 CAPTURE EXIT CODES OFF A PIPE AND YOU MEASURE `tail`.** I reported `RUNALL REAL EXIT: 0` for a
  run whose own log said `FAILED (2)`. `$?` after a pipeline is the LAST stage. Both instruments are
  now separate statements with the exit captured immediately. Full account: memory
  `project_exit_code_after_a_pipe.md`. **The tidying is what introduced it** — both runs were
  correct until I combined them into one block for readability.
- **`tests/test_command_palette.py` CANNOT RUN STANDALONE** (13/15 fail alone at HEAD). Unchanged.

## 4. OWED — SPLIT BY OWNER

**MINE** — nothing.

**RYAN'S** — **one press of shift+tab** (§2). That is the acceptance criterion for the keybinding,
not my green.

**OPENBOLT'S** — nothing to me. His `a5a5511` is validated here: `pytest -q tests/` 1322/0 and
`run_all.py` REAL EXIT 0 (1317 passed, 117 files + 13 scripts), measured with my work stashed out so
a red could not be misattributed. His correction to handoff-d §2 is folded in AT THE CLAIM.

**UNOWNED / DISCOVERED** —
- ✅ **`tool_policy.cycle()` fail-open: FIXED in `f8845ac`.** It returned `AUTONOMOUS` for an
  unrecognised name while `unattended()` sent unknown to the floor. **Not cosmetic:**
  `action_cycle_tool_profile` WRITES the cycled value back to settings and SAVES it, so a corrupt
  value plus one shift+tab promoted the STORED authority to maximum and persisted it. Found by
  re-reading my own diff, not by a red — the unknown branch had no coverage at all.
- `Job.tool_profile` is a **dead knob**: no longer read when a job fires, still persisted, still set
  by `goal_loop.py:456`. Deleting it is safe (`scheduler.load` filters to `__dataclass_fields__`)
  and is a schema change that did not belong in an authority fix.
- `goal_loop.py:456` hardcodes `tool_profile=SCHEDULED` while `goal_loop.py:365` reads the setting —
  one file expressing the user's authority two ways.
- `pyproject.toml` / `uv.lock` carry someone else's `websockets>=15` (chrome-bridge). Untouched.
- Nothing drives `_stream` with reasoning AND `show_thinking` on (from handoff-d, still unowned).

## 5. ABSENT BY DECISION — AND THE GATE THAT DEFENDS EACH

- **`Job.tool_profile` NOT deleted.** Gate: `test_an_old_job_file_carrying_the_dead_key_still_loads`
  plus its rewritten docstring. The brief said delete "if nothing sets it"; something does.
- **The footer level has NO settings toggle.** T084 happened because the authority in force was
  invisible. Gate: `test_absence_renders_as_ABSENCE` + the field's unconditional placement.
- **`unattended()` kept after the default moved to `autonomous`.** It still fires for a user who
  explicitly picks `interactive` and then receives mail. Gate:
  `test_an_explicitly_chosen_interactive_still_degrades_unattended`.
- **The binding is `priority=True`, against the brief.** Without it Textual's own Screen binding
  wins and the feature does nothing anywhere. Gates: `test_shift_tab_cycles_the_level_at_app_level`
  AND `test_a_dialog_keeps_its_own_shift_tab` — **neither alone is sufficient; each is satisfied by
  a different wrong build.**

## 6. MY OWN CORRECTIONS AND RETRACTIONS

1. **I generated a false green and it was my own instrument.** `$?` after a pipe. §3.
2. **I did not predict the default change's blast radius: 12 tests, three files that never mention a
   profile.** They exercised the approval/deny machinery and were inheriting `interactive` from the
   default. A test that depends on an ambient default is testing two things and naming one.
3. **Setting the profile on `settings` alone is not enough** — `_active_tool_profile` is stamped at
   CONSTRUCTION and the tool door reads that. Bit me while fixing #2; the same mechanism is what
   makes the new cron assertions non-vacuous.
4. **My own change created a drift I then had to close.** Moving the default to `autonomous` left
   three `INTERACTIVE` fallbacks in app.py that had agreed with the old default *by coincidence*;
   afterwards one pointed the PERMISSIVE way on the path where authority is unknown.
5. **I argued against Ryan's `_fire_job` ruling and was overruled** (retroactive re-authorisation of
   saved jobs). Implemented as ruled; the concern is recorded, not re-litigated.
6. **A backticked inline inbox message ran as command substitution and ate a clause** — the CLI's
   own `--help` warns about exactly this and I had read it that session. Corrected with
   `--body-file` (`d115a97c`). Reading a warning is not having a habit.
