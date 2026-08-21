# Changelog

LiteTUI — a local-LLM TUI harness with per-conversation memory, a fleet seat,
and a real tool loop.

Versions below **0.7.0 were assigned retroactively** on 2026-08-20 from the git
history. They mark real milestones in that history, but they were never tagged
or released at the time — the project had no version at all until this file
existed. Everything from 0.7.0 onward is assigned as it happens.

Format follows [Keep a Changelog](https://keepachangelog.com). This project
uses semantic versioning, pre-1.0: interfaces still move weekly.

The version itself lives in `version.py` and nowhere else. `tests/test_version.py`
fails if this file's top released heading disagrees with it.

---

## [Unreleased]

_(nothing yet)_

## [0.10.0] — 2026-08-21

### Added
- **The SHADES family — ten truly-gray dark themes** (obsidian, graphite,
  onyx, charcoal, gunmetal, slate, smoke, ash, pewter, iron). "It should be
  50 shades of gray" is enforced, not promised: a test bounds the channel
  spread of every core color (surfaces, text, primary, success ≤ 16/255), so
  a colorful value cannot sneak in — it caught two of the author's own on the
  first run. Semantics follow amber-ledger's rule generalized: success is a
  GRAY (healthy is colorless), warning a sand-gray, and only error keeps
  enough desaturated brick to be findable. Nothing neon, no orange, no green.

### Removed
- **Light themes stripped from the picker** (textual-light, catppuccin-latte,
  solarized-light, rose-pine-dawn, atom-one-light, and terminal-relative
  textual-ansi) — unregistered at boot and excluded from settings choices,
  with a guard so a saved light name falls back instead of crashing.

## [0.9.0] — 2026-08-21

### Added
- **Themes: the LiteSuite palette, ported** (`themes.py`) — all 12 presets
  from `LiteSuite/apps/web/src/litesuite/lib/themes.ts`, including **Matrix**
  (phosphor on black) and **Lite Suite** (gold on graphite), registered
  beside Textual's built-ins. The port is generated from the source file and
  a cross-repo test re-extracts and compares every token, so a hand-edited
  hex fails with the field named — "source of truth" as a gate, not a wish.
  Amber Ledger's success color is deliberately NOT green, per its upstream
  design rule, and a test guards the temptation.
- **The theme choice persists.** Picked from the command palette (ctrl+p →
  "Change theme") or the new /settings dropdown; either way it survives a
  restart. Before this the palette's pick silently reset to textual-dark
  every boot — a working control whose effect evaporated.


## [0.8.0] — 2026-08-21

### Added
- **Three modes for what a tool's output costs the conversation, WIRED**
  (`tool_context.py` + `_contextualise_tool_result`) — `off` (raw, the
  baseline), `llm-tool-mask` (observation masking: placeholder naming tool,
  size and sidecar path; no model call), `llm-tool-summ` (a side call
  summarises toward the task; the main conversation never holds the raw even
  once). Both processing modes park the raw under the conversation's own
  `tool-raw/` and the placeholder carries the path, so the model can `read` it
  back — the modes differ only in what reaches context, never in what
  survives. A failed or empty side call degrades to the mask, not to loss.
  Selected in /settings; results under the threshold enter verbatim.
- **Mid-turn message queue with a swappable interrupt chord.** A message sent
  while the agent is mid-turn no longer cancels the turn (that was never a
  feature — it was `@work(exclusive=True)` doing what exclusivity does).
  Default: Enter QUEUES (held, visibly, sent as a real turn when this one
  ends) and ctrl+shift+enter INTERRUPTS (partial kept, your message next).
  One setting swaps the two ends. Inbox mail from other agents queues by the
  same path — measured this week: mail appended mid-turn sat INERT in context
  for four turns while the model's own inbox tool truthfully reported "(no
  new messages)", because this monitor had already claimed it.
- **A tool cancel button** (top-left, beside the palette icon; visible only
  while a bash subprocess is actually running). Kills the process TREE —
  `shell=True` makes cmd.exe the child and the real work its grandchild, so a
  naive kill orphans exactly the thing being cancelled. The turn CARRIES ON:
  the model receives `[cancelled by user after Xs]` plus whatever the tree
  wrote before dying, and can react. Esc still stops the whole turn — two
  different verbs, deliberately. Verified against the process table with a
  negative arm, per Docs/spec-tool-cancel.md.
- **Live elapsed + tok/s on the thinking header** (6647a25) and **a projected
  ETA on the in-flight bubble** (578b436), the latter gated so a KV-cache-hit
  turn can never teach a misleading rate.
- **The seat's fleet id now derives from the conversation id** (0852dab), so
  resuming a conversation keeps its identity instead of minting a stranger
  and leaving a ghost heartbeating at nothing — one conversation produced
  three ids in one evening, and a task dispatched to the id last seen was
  silently never delivered.

### Fixed
- **Auto-compact can now fire INSIDE a turn** (464f72e). Both checks sat at
  the agent loop's `return` statements, so a turn that kept calling tools
  sailed 80 → 84 → 87% without one check; with `tool_iterations: 100` a
  single turn could eat the window. The loop now tests between iterations
  and breaks — it never starts the compaction itself, because `_compact` is
  exclusive in the same worker group and would cancel the loop that called
  it. A failed compaction also no longer retries identically with no backoff.
- **Bash timeouts now kill the process tree** — the old path reported
  `[timed out after Ns]` while the real work kept running detached.
- **Model and user text is never markup-parsed on its way to the screen**
  (885aa7a). Four sites assigned raw strings to `.content`; streamed text
  containing `[key=` matched Textual's tag grammar and killed the turn with
  `Expected markup value`. Most bracket text fails the grammar, which is why
  weeks of bracket-heavy bubbles proved nothing.

---

---

## [0.7.0] — 2026-08-20

Correctness night: two false-failure sources removed and one long-running race
closed.

### Added
- `wake_after_compact` — loop mode resumes the in-flight task after a compaction
  instead of going quiet. (`f5b17f0`)

### Fixed
- **Eight tests reported as failures that had already passed.** The child's
  stdout pipe encoded as cp1252 on Windows, so a red-circle emoji in a label
  raised `UnicodeEncodeError` *after* every check had succeeded. The tests were
  never wrong. (`4aa4f20`)
- **The AskUserQuestion step bar could paint with no active step.** The label's
  `active` class was set only in an async `on_mount`, so a caller checking the
  moment the screen was pushed saw it missing — 2 pass / 4 fail over six runs.
  Now set at compose time, matching what the body already did. 8/8 after.
  (`140b440`)

---

## [0.6.0] — 2026-08-20

Context accounting and seat identity stop lying.

### Fixed
- A stale context window persisted for the whole session, and auto-compact
  stopped firing entirely once the re-read was wired into one of three loop
  exits. (`15df95d`)
- The context length set in settings was never applied on a model switch.
  (`9917585`)
- **A resumed prompt told the model it was a seat that no longer exists.** The
  agent id is minted per process; the fleet-identity sentence is written into
  the system message after registration, so a resumed conversation replayed a
  dead process's id — and the same prompt tells the model to answer mail as
  that id. (`f75fd45`)

### Added
- Chrome relay owns its own lifecycle (`start`/`stop`/`status`), gains
  `write_text`, and a constant left pointing at a pre-move path is repointed.
  (`9c112ba`)

---

## [0.5.0] — 2026-08-20

Resume, skills discovery, and a test suite that could survive its own repo.

### Added
- `/skills` makes discovery visible; scanning several libraries takes the
  catalogue from 1 to 77, and 13% of them stopped arriving unlabelled —
  `description: >-` block scalars were being read as the value. (`c4c8713`, `a70f398`)
- Resume shows the conversation uuid and its owning seat. (`55001fa`)
- The footer shows context percent, and every field is hideable. (`68909f3`)
- A test runner that knows pytest-style from script-style files — the two
  cannot be run the same way, and running one as the other reports failure on
  working code. (`e8e6502`)

### Fixed
- **Never load model weights as a side effect.** Connecting was loading a saved
  preference's weights on every boot. Conversations are created lazily.
  (`8df2b9e`)
- **HEAD did not contain the app.** `tools/` and `ask_user_question.py` were in
  zero commits while the code needing them was committed — a fresh clone lost
  both. (`cda355b`)
- The tools/ move silently unregistered pccontrol and chrome; the existence
  gate was working perfectly against a pre-move constant. (`f35d332`)
- **The test suite was evicting the running app from the fleet.** Registration
  passed `--takeover`, which does not spare a live holder. (`a1e8686`)
- The seat now heartbeats — a live pid was not enough to stay on the roster.
  (`321f631`)

---

## [0.4.0] — 2026-08-20

Settings become real.

### Added
- `/settings` panel, auto-compact, `/clear-screen`. (`ab7ae6b`)
- Tabs, one per section, each scrolling independently. (`b41498c`)
- Tests that drive `/settings` and `/clear-screen` through a real app.
  (`229ceff`)

### Fixed
- **10 of 30 controls were dead** — wired, then guarded so they cannot silently
  die again. (`e4b235d`)
- The panel was docked top-left instead of centred. (`a51e7e2`)
- A new thinking block now jumps the log to the bottom. (`2435dc1`)

---

## [0.3.0] — 2026-08-19

Tools, and an envelope with no bypass.

### Added
- `view_image` — and it deliberately does not return the image. (`6b781ed`)
- `pccontrol` and `chrome` as verbs; the wrappers exist for the traps.
  (`b33109e`)
- ttyguard phase 2 — the child-process envelope now has no bypass, and the scan
  is green. (`ee87616`)

### Fixed
- One system turn, not two — qwen/qwen3.8-27b rejects the second. (`04fe623`)

---

## [0.2.0] — 2026-08-19

The fleet seat becomes a participant rather than a listener.

### Added
- **The agent gets the fleet verbs.** It could receive mail but not answer.
  (`3fdbc15`)
- The footer carries the seat's fleet identity. (`e86e7af`)
- tok/s in the footer. (`f743dc6`)
- ttyguard phase 1 — one envelope for every child process. (`f5779bc`)

### Fixed
- The seat reclaims its own name across restarts and reports the real one.
  (`2051d7f`)
- The seat passes its own pid, so the fleet can tell it from a corpse.
  (`1ec2aa2`)
- Terminal escapes stripped from tool results; mouse modes re-asserted.
  (`a482737`)
- Autoscroll follows the stream; thinking-off no-ops became visible.
  (`747ea2d`)
- `Seat.send` no longer passes `--priority`; this CLI has no such flag.
  (`94e59a1`)

---

## [0.1.0] — 2026-08-18

First commit 2026-08-18 23:42.

### Added
- **LiteTUI** — a local-LLM TUI harness with per-conversation memory.
  (`7046082`)
- MCP servers, skills, a harness seat, and an end to re-injecting the store.
  (`c919214`)
- Clickable modals for `/model`, `/help` and the stop dialog. (`ee5c544`)

### Fixed
- Branded as LiteTUI, and Ctrl+V / Ctrl+X actually reach the app. (`bbb967e`)
