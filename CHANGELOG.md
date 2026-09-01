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

### Added

- **Dual-backend model control.** Two engines behind one seam
  (`src/llm_backend.py`): LM Studio desktop (control plane moved from the
  `lms load` shell-out to the official `lmstudio` SDK — load with context
  length, and for the first time UNLOAD) and **our own `llama-server` in
  router mode** — one process, a generated `--models-preset` ini covering
  every chat GGUF on the box (LiteSuite's install, LM Studio's dirs, the HF
  cache, custom roots; deduplicated; voice/embedding ggufs filtered by the
  file's own `general.architecture` header, never by name). Attaches to
  LiteSuite's server on :8088 when healthy, else spawns its own on :7470
  (detached, console-safe, log file, killed with the app). Boot loads
  NOTHING on either engine; `--models-max 2` guards VRAM.
- **`/backend`** (switch engines mid-conversation — history survives),
  **`/load`**, **`/unload`**, and **`/modelcfg`** — a per-model
  Info/Load/Inference screen with LM Studio panel parity: ctx, GPU offload,
  threads, batches, parallel slots, flash-attn, KV quantization, mlock/mmap,
  RoPE, seed, draft-model speculative decoding, vision mmproj, chat
  template, per-model sampling overrides layered over the global /settings,
  structured output (JSON schema), and named presets. Controls an engine
  cannot drive render greyed with the reason — never silently absent, never
  fake. A one-time engine picker runs at first boot when both engines are
  detected.
- The studio tool's seat suspend/resume now routes through the active
  backend (llama router natively; an attached server refuses honestly), so
  GPU generations free the right VRAM on either engine.
- **grep + edit tools** (`25e7262`) — ripgrep-via-argv search and guarded exact-byte edits. _draft · Ryan-copy pending_
- **listen tool** (`20aa2e4`) — audio perception via local Qwen2-Audio, one seat-swap per listen (banked from EvenCurve). _draft · Ryan-copy pending_

### Changed

- **The theme palette is now independent of LiteSuite's**, by Ryan's ruling
  2026-08-28: *let them diverge*. Every preset began as a token-for-token port
  of LiteSuite's `themes.ts`, and that port is now a historical fact rather
  than a live dependency — the values are frozen, and an upstream theme edit
  is no longer a defect here. The cross-repo drift gate that enforced the old
  contract is deleted; `themes.py`'s header carries the new one, including the
  divergence that occasioned it (`matrix.panel` #0D0208 here vs #04170A
  upstream, LiteSuite `cb071ea4`). Do not re-sync.

### Fixed

- **studio image-tool extension** (`6b207e3`) — full image-param passthrough (width/height/steps/guidance/negative_prompt/seed/sampler/batch/img2img) with guidance pinned to LiteImage's 3.5; banked together with its stale-test fix. _draft · Ryan-copy pending_
- Setting top_k, min_p, or repeat_penalty in /settings would have crashed
  every request: the OpenAI client's `create()` has typed params and no
  `**kwargs`, and the sampling dict was passed at the top level. Non-native
  sampling fields now ride `extra_body`.
- **The test suite could read and write the developer's own `settings.json`.**
  `conftest.py`'s `_never_write_the_live_settings` is a pytest autouse fixture,
  so it never reached the 13 script-style test files, which run as bare
  `python tests/x.py` — the same 13 `pytest -q` cannot collect, so CI never saw
  the consequence either. Four of those boot a real app and so loaded the live
  file; `test_settings_live.py` drives `/settings` through it, which is the
  exact route that once reset `tool_iterations` 100 → 48. All four now redirect
  settings to a sandbox and **assert the redirect took**, via the new
  `tests/_script_guard.py`. Evidence: the live file's md5 is unchanged across a
  full `run_all.py` run.
- The `dialog_style: "sidebar"` path — the shipped dialog mode — had no passing
  test. Two files asserted the modal path while reading whatever the developer
  happened to be running, so the suite went red on a correct build. Both now
  pin the configuration they test.
- `README.md` told new users to run `python test_convos.py`, a path that has not
  existed since the runtime moved under `src/` (`ca6fef9`), and never mentioned
  the llama.cpp backend at all.

## [0.21.0] — 2026-08-21

### Changed

- **The plugin split.** app.py went from 5,914 lines to ~4,300 by becoming a
  HOST: every capability now ships as a plugin in `src/plugins/` — the four
  floor tools (critical: their absence aborts boot), view_image, pccontrol,
  chrome, ask_user_question, studio, skills, harness, MCP (as an args-only
  dynamic provider — unifying the registries must not widen MCP's reach),
  themes, mark, misc, conversation lifecycle, model switching, help,
  settings UI, and the whole scheduler (calendar screens moved whole).
  The substrate (`plugins/__init__.py`) enforces one-owner-per-fact at load
  time: duplicate tool names, command tokens (aliases included), and
  prompt-order slots are refused, never shadowed. Per-instance registries —
  the suite builds many apps per process and they share nothing.
- **The palette derives.** LiteTUICommands rows come FROM the command
  registry (plus two explicit escape hatches); the hand-written second
  table — the drift class a source-grep test existed to police — is gone.
  The old drift gate was proven tautological by mutation and rebuilt to
  test what can still fail: derivation completeness and the one embedded
  command string nothing else exercised.
- **One home for path anchors** (`paths.py`, module-imported everywhere —
  a from-import copies the binding, which is how a live-store guard patches
  one home while code reads another) and for the shared formatter
  (`fmt.py`), the picker modal (`picker.py`), and THINKING_LEVELS
  (settings.py).

### Added

- **`/plugins`** — every plugin with its true status (active / disabled /
  failed: reason), host-registered so the readout cannot be disabled away
  with a plugin. **`settings.plugins_disabled`** skips non-critical plugins
  at boot (mirrors `mcp_disabled_servers`; hand-edited in settings.json for
  now — a per-plugin checkbox UI is a named descope).
- **Permanent gates:** the dogfood test (real capabilities counted AS
  plugins, by owner) and the re-accretion guard (app.py may import the
  substrate, never a plugin module — the road back to monolith fails CI).
  The ttyguard envelope sweep now recurses into `src/plugins/`, proven by
  a planted violation before any plugin file existed.

### Fixed

- `/mark` had a latent crash (`tempfile` import swept out with a moved
  block) — caught by a negative control that ran an enabled plugin's
  command for real; and the ask_user_question script test was found leaning
  on the old set_app global's construction-order accident — each section
  now wires its own app, which is the per-instance registry's argument
  made flesh.

## [0.20.0] — 2026-08-21

### Added

- **Glass-box compaction.** Every CLI treats compaction as a spinner and a
  prayer; LiteTUI now renders the whole act, live, in the same grammar as a
  normal turn — a `CompactionCard` marked with the warning hue, carrying:
  the plan (N messages → summary, keeping K verbatim), the EXACT compaction
  prompt in a collapsed fold, the model's thinking as a real ThinkingBlock,
  every store write as a real ToolMessage (the most interesting part of a
  compaction is the model deciding what is durable — previously invisible),
  the summary STREAMING into a markdown body, and a ledger
  (messages/chars before → after, persisted files). Autocompacts are
  marked `· automatic`. Failures are written ON the card, not swallowed.
- The compaction request itself now streams (`stream: True`) — the card's
  progress is the stream, not a fabricated percentage.
- `FoldBlock`: a generic collapsible for static payloads (ThinkingBlock's
  shape minus the timer, collapsed by default).

### Fixed

- `ThinkingBlock.reset_header` no longer dies when a fast stream ends the
  trace before the block's children compose (a pre-compose reset is a true
  no-op — caught by the render pass, not the tests).
- The compaction card is awaited onto the chat log before the stream is
  consumed — the first chunk can arrive before compose() runs.


## [0.19.0] — 2026-08-21

### Added

- **The seat suspends itself for generation** (`src/seat_guard.py`). The
  agent IS a ~29 GB resident in LM Studio, and the generation it orders
  needs that VRAM — loading beside it near-OOMed the workstation once.
  On `generate` actions the studio tool now: records the seat's live load
  config from `lms ps --json` (identifier, context, parallel), refuses if
  the model is not idle or has queued requests (parallel=4 means OTHER
  seats may be mid-stream), unloads, runs the generation TO COMPLETION
  (sound's async job is polled inside the call — the poller is the thing
  that got unloaded), then reloads at the exact recorded config.
- Safe because an LM Studio model is only busy DURING a completion: tool
  execution happens between completions, while the turn waits. The
  conversation lives in LiteTUI, not the model — what cannot survive is
  the KV cache, so the first reply after a resume re-ingests the prompt
  once. The spec says so, and the tool's result reminds the model to
  expect (and explain) the slow first token.
- Resume is the load-bearing arm: it runs in a `finally` whatever the
  generation did, retries, verifies the loaded context MATCHES the record
  (the JIT-default trap loads at the wrong window), never re-arms a TTL
  the human didn't choose, and on persistent failure leaves
  `suspended_seat.json` plus the exact `lms load` command in the
  transcript — because the model that would normally read the error is
  the thing that is missing.
- Lookup actions (status, job, gallery, models, config) never touch the
  seat. No seat injected, or seat not resident: generation runs plainly.


## [0.18.0] — 2026-08-21

### Added

- **The `studio` tool: local generation from the chat.** One tool, three
  local apps — `image` (LiteImage REST, :7426), `sound` (LiteSound REST,
  :7427, async job flow), `model` (LiteModeler via the `lst` CLI —
  prompt-to-3D). No API keys, no cloud; the machine's own GPU does the work.
  The spec description carries the working knowledge the model needs — the
  async job_id/poll contract, that the apps must be RUNNING, and to tell the
  human rather than retry blind. Offered unconditionally: availability is
  runtime state, and the tool reports its own precondition in one honest
  sentence.
- End-user correctness by construction: image/sound address PORTS (env
  overridable), so an installed app and a dev checkout are indistinguishable;
  model delegates to litesuite-tools, the single owner of find-the-CLI
  (companions: LiteSuite `1f8e16ee` — installed-copy probing +
  Electron-as-node + 17 vendored handlers that a blanket gitignore had
  silently untracked; LiteModeler `cade477` — the installer now ships the
  CLI).

### Fixed

- The `lst` spawn initially bypassed the ttyguard envelope — caught by the
  envelope sweep in test_ttyguard.py, the gate built for exactly that. Routed
  through `ttyguard.run`.


## [0.17.0] — 2026-08-21

### Added

- **The command palette knows the app.** Ctrl+P carried Textual's five stock
  commands and none of LiteTUI's — 90% of the features were undiscoverable
  from it. A `LiteTUICommands` provider now serves fifteen rows (calendar,
  new scheduled job, scheduled jobs, settings, model switch, thinking level,
  new/list conversations, compact, skills, mark the screen, tool toggle,
  clear screen, reconnect, help), each routed through the SAME
  `_handle_command` the keyboard uses, so the palette cannot grow behaviour
  of its own. A drift gate walks the table against the dispatcher source: a
  renamed command breaks a test, not a palette row.
- **The working directory is in the header** (home shown as `~`).

### Fixed

- **The header's tool count was a hardcoded literal.** `tools:4` was written
  when there were exactly four tools and stayed 4 while view_image, chrome,
  pccontrol, ask_user_question, skill, harness and the MCP set arrived. The
  count now derives from `_all_tools()` — the list the model is actually
  offered — and repaints when the harness tool joins at seat registration
  (the one tool that arrives late).
- The gate for the old literal matches the assignment, not the string: the
  comment retracting `tools:4` quotes it, and a bare grep would flag the
  retraction as the defect.


## [0.16.0] — 2026-08-21

### Added

- **A human schedule builder in the job editor.** A pattern dropdown (every
  day / weekdays / weekends / weekly / monthly / yearly / every N minutes /
  every N hours) with number tickers and selects — `−`/`+` buttons, arrow
  keys, direct typing, midnight wrap. The raw cron field became the **Custom**
  preset: still there, prefilled with whatever the builder built, so the form
  quietly teaches cron. The generated string and its live next-fire stay
  visible on every change.
- The builder is **presentation only**: it writes into the same input the
  save path has always read. The store, the tick loop, `/cron add` and the
  calendar are untouched.
- `recognize()` is a **proven inverse** of `build()` — 205 pure tests sweep
  every preset × edge params both ways — so opening an existing job populates
  the widgets from its string, and anything the builder does not fully
  understand (dow lists, dom+dow combinations, stepped minutes with fixed
  hours) opens honestly as Custom rather than being guessed at. The
  calendar's create-from-day prefill now opens as a legible "every year on
  August 15 at 09:00".
- `NumberTicker` (src/ticker.py): a reusable terminal spinner — typing and
  ticking both first-class, display normalised on blur, never mid-keystroke.

### Fixed

- Textual's `Select` echoes its initial value as a `Changed` during mount;
  treating the echo as a user action rewrote an untouched `@daily` to
  `0 0 * * *` on save. Guarded by VALUE, not timing: an event carrying the
  value already held is definitionally not a change. Untouched jobs now keep
  their exact string, aliases included.


## [0.15.1] — 2026-08-21

### Fixed

- **The day popup and job editor docked to the top-left** instead of
  centering. The house centering rule is one CSS selector list, and the three
  calendar screens were never added to it — CalendarScreen hid the defect by
  being 96%×92%, where docked and centred look identical. New gate:
  `test_modal_centering.py` asserts the POSITION of every modal box (equal
  gaps both axes, in a live app), so the next screen that forgets to join the
  selector fails a test instead of a screenshot.


## [0.15.0] — 2026-08-21

### Added

- **The calendar is clickable.** Every day cell opens a popup of that day's
  jobs; each job opens an editor; every day carries a create row, so an empty
  day is an invitation rather than a dead end. Side-pane jobs are clickable
  too. Full keyboard equivalents: arrows/enter in the popup, `n` for new,
  enter-in-any-field saves the form, escape cancels.
- **Job editor** with a live schedule preview: the next real fire of what is
  typed (including the "next year" a passed date resolves to), the per-day
  fire count, and — when it does not parse — the exact field that is wrong.
  Save refuses an empty prompt or a broken schedule with the reason visible.
  Delete takes two clicks, and the Button's own pressed-effect debounce means
  a hardware double-click cannot blow through both stages.
- Mechanism: a **hit-map built beside the paint** — the loop that emits each
  grid line appends one map entry, so the map cannot drift from the drawing.
  Verified by a triangle test (map says (x,y) → paint shows the digits at
  (x,y) → a real click there opens the day) and a negative control: a map
  shifted one line fails both legs.

### Fixed

- The day popup sorted by time alone, so a DISABLED 00:00 job outsorted every
  working one and Enter edited a job that will not fire. It now ranks what
  WILL happen: running jobs in time order, then disabled, then broken. The
  ordering test's fixture had no disabled job and could not see this — the
  render did.


## [0.14.0] — 2026-08-21

### Added

- **Cron jobs and a scheduler** (`/cron`). A scheduled prompt is an input
  nobody typed, so it rides the same held-then-flushed path as inbox mail
  rather than a second delivery route. Standard 5-field cron plus `@daily`
  and friends; `add`, `list`, `rm`, `on`, `off`, `run`.
  - Jobs fire **only while LiteTUI is open**. There is no OS-level
    registration; the store is plain JSON so a headless runner could consume
    it later.
  - A slot missed while the app was closed does **not** fire late. Waking to a
    burst of overdue prompts is worse than missing them: the burst arrives with
    nothing marking it as late and gets answered as though it were now.
  - The cron worker has its **own** worker group. In `chat` every tick would
    cancel the turn in flight — the trap autocompact fell into.
- **A month calendar** (`/calendar`, `/cal`) in the style of
  [calcure](https://github.com/anufrievroman/calcure), which is curses-based
  and therefore Linux/macOS only. This is a rebuild of the look in Textual, so
  it runs on Windows: calcure's icons and semantic colours, its cell maths
  (pane ÷ 7 wide, six week rows, height ÷ 6 tall), resolved through the active
  theme instead of fixed ANSI indices.
  - Day names fall back to `MON`/`TUE` when the cell is too narrow for the
    full word — truncating gives `WEDNE` and `THURS`, which are not words.
  - A day with more jobs than fit says `+n more` instead of dropping them. A
    calendar that hides an entry tells you the day is free.

### Fixed

- **`pytest` collected ZERO tests for the whole repository.** One test file
  ended in a bare `asyncio.run(main())` at module level, so the collector
  imported it, ran it, and took a `SystemExit`; 13 of 47 files were in that
  state. Nothing noticed because the scripts were all *passing* — they run
  standalone and exit 0, so the signal everyone looked at was green and the
  entry point nobody typed was dead. `conftest` now derives `collect_ignore`
  from whether a file defines `def test_`, and `test_collector_integrity.py`
  fails if a collected file ever runs itself on import.
  - Suite: **no tests collected → 393 passing.**
- A day's `+n more` counted against the times *fetched* rather than the one
  *displayed*, so a job firing 48 times a day read `+46` while 47 were unshown.
  The test asserted the wrong value too, which is why it survived until the
  render was looked at.


_(nothing yet)_

## [0.13.0] — 2026-08-21

### Added
- **/mark — a manual human screen-marker channel.** The marker overlay grew
  an interactive mode: a draggable ring with send/cancel buttons riding
  under it. Drag it onto the thing, click send — the overlay captures that
  monitor WITH THE RING STILL IN THE SHOT (the ring is the highlight; only
  the buttons hide), writes an atomic JSON handoff with both absolute and
  monitor-local coordinates, and LiteTUI ships screenshot + coords to the
  model as a user turn. Mid-turn it queues like inbox mail instead of
  cancelling the turn; the wait runs in its own worker group for the same
  reason. The agent-facing timed click-through marker is untouched.
  Live-drilled end to end: the reported center matched the optically
  measured ring position within one pixel, and the drill caught a real bug
  inspection missed — PowerShell writes JSON with a BOM.

## [0.12.0] — 2026-08-21

### Added
- **A terminal color picker in the theme creator** ("hex isn't enough").
  Saturation/value field with half-block doubled resolution, hue bar, the
  current theme's tokens as clickable presets, live swatch, arrow-key
  nudging — and the hex box stays as an editable escape hatch whose typed
  value wins on Enter. Every geometry mapping (click→cell→color) is pure
  and round-trips per cell, because a picker that returns a slightly
  different color than the one under the cursor lies with confidence.

### Changed
- **The runtime moved to `src/`** — run.bat stays in the root. Data
  (.convos, settings.json, skills/, systemprompt.md) stays at the repo
  root; all seven path anchors were re-based and proven by resolution.

## [0.11.0] — 2026-08-21

### Added
- **A custom theme creator, in a new Themes settings tab.** Ten token fields
  (6 accent/semantic + 4 surface/text — the full schema in
  themes.THEME_TOKENS), prefilled from the CURRENT theme so you start from
  the nearest neighbour and nudge. Name + Ctrl+S validates every hex
  (naming the bad field in the error), creates the theme, SELECTS it, and
  persists it to settings.json. An existing custom name is overwritten — the
  creator is the editor. Corrupt hand-edits cost one theme at boot, loudly,
  never the app.
- **The theme picker moved to the Themes tab** (with the creator); the
  command palette's ctrl+p quick-select stays.

### Fixed
- **Dict settings fields survive the round-trip.** settings._coerce's str()
  fallback turned a loaded {} into the STRING "{}" — caught by the existing
  round-trip contract test the moment custom_themes landed. Same
  type-roundtrip class as the known skill_roots comma-string bug, which
  remains open.

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
