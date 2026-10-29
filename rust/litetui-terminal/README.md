# LiteTUI Rust — interactive live chat and offline preview

**Interactive text chat is implemented; full parity is not complete.** `--interactive` connects the existing composer and transcript to one real owned Python runtime for multiple turns. `--live` remains the one-prompt streaming smoke path. Default / `--preview` remains an explicitly offline mock conversation.

## Runtime management connection

Live startup requires matching protocol/data versions, advertised hello/state operations, and a valid idle snapshot whose session, backend, model and profile match readiness. This read does not grant authority or prove persistence, and a fresh pending store may report writable false. Only bounded startup facts are retained; the snapshot's raw settings are not displayed or retained in view state.

After negotiation, each source raw event is paired with its GUI envelope and only the envelope reaches the transcript. Equal events remain separate occurrences. Pair tracking accepts cross-thread interleaving, caps unmatched records at 64 / 4 MiB serialized payload, rejects unmatched EOF/new prompt, and applies a deadline no longer than twenty seconds even while chat is idle. This supplements the existing frame, pipe, transcript and display-work limits.

Native and generic turn producers must carry the captured execution/session/operation/hook/source origin. Late or changed origins fail before content reaches another turn. Approval/question closures retain their separate request ledgers; their ambient operation may be the cancellation command. Model changes outside an owned management operation remain unsupported. Startup snapshots never settle turns or overwrite newer stream facts. Model/thinking controls are described below; later snapshot refresh and the remaining management screens are still pending.

## Native model and thinking selection

In idle live chat, **F3** opens models; **Tab** switches between model and thinking choices. The thinking footer opens thinking choices directly. Up/Down or a paired left-click selects a row; a fresh Enter applies it. Left/Right inspect long values and source explanations. R refreshes; Escape closes. The saved draft, paste and clipboard remain isolated. Choices cannot be confirmed while hidden, waiting, resized away, or held from an earlier key press.

The connected runtime supplies the catalogue, allowed thinking levels and current selection. Each apply is followed by a separate readback before another prompt can be admitted. Failed requests show the actual state read back; an uncertain readback stops the connection. There is no automatic retry or rollback. Closing after a request was sent does not cancel it or release the composer early. A verified model change clears the previous model's context/rate/thinking facts until new facts arrive. Backend/engine switching is not implemented.

The controls cap retained metadata at 64 KiB, model rows at 512 and thinking rows at 32. Source values are escaped only for rendering. The default thinking choice means the Python runtime's default selection; reported capability levels do not prove provider support or an inference effect.

**Verification (2026-10-29):** 319 ordinary Rust tests and 31 controlled-process tests passed, with fmt/Clippy/build/CLI help. The real-source controlled process verifies setters, owned persistence, failed-write readback and the next turn's selected values without contacting a provider. Affected Python coverage has 112 unique passes and two reproduced baseline fixture failures (test_choice_survives_conversation_reopen, missing owned_session). Eight new source regressions pass; seven fail against committed source functions. Lint/type checks add no findings against committed HEAD. Actual provider behavior and hands-on terminal acceptance remain pending.

## Open interactive live chat

Run in a terminal:

```powershell
& 'C:/Projects/.worktrees/OpenBolt-rust-tui/rust/litetui-terminal/open-chat.ps1'
```

To enable source-authorized tools in a chosen workspace:

~~~powershell
& 'C:/Projects/.worktrees/OpenBolt-rust-tui/rust/litetui-terminal/open-chat.ps1' -Tools on -ToolProfile strict -Workspace 'C:/Projects/your-project'
~~~

Tools default off. Without -ToolProfile, a native picker asks before Python starts; Escape or Ctrl+Q cancels without creating runtime state. Arrow keys or a click select a row, and a fresh Enter confirms. Redirected launches must supply --tool-profile. Strict asks before sensitive actions; interactive and autonomous are also explicit source-profile choices. The runtime must confirm the requested mode/profile, workspace and approval-host route before any prompt is sent. Startup also negotiates management protocol 1 and reads the actual source state before enabling the composer. Older sources without this contract fail with an update diagnostic. External MCP is still off. This launcher creates fresh data; existing-session resume is not implemented.

Or double-click **`open-chat.cmd`** for a new dark Windows Terminal window. Both build current source and start Claude `claude-sonnet-5-5` in a new retained `C:/Projects/.scratch/T0565/claude-chat-<guid>` root. No local model service is queried or started. Pass `-Python <exe>` to the PowerShell launcher to override the interpreter.

- Type a prompt; **Enter** sends it. Replies stream into the existing themed assistant-card geometry. Subsequent prompts retain the real Python/Claude conversation.
- **Esc** while a turn is active opens stop confirmation; **Ctrl+Esc** reaches it during a question or approval. Keep going is selected by default. A second confirmed stop forces interruption. The session waits for source settlement before accepting another prompt, retains partial output, and never replays the stopped prompt. Stopping does not undo earlier tool effects.
- **Ctrl+Q** requests shutdown and waits for the contained runtime process tree. Draw/input errors also run an unconditional cleanup epilogue; UI and cleanup failures are both preserved.
- While a turn is busy, another Enter preserves the draft rather than queueing/retrying it. Live submission preserves composer whitespace. History, editing, selection, clipboard, scrolling and **F4** in-memory themes reuse the existing implementations.
- The footer now projects validated runtime facts: reported model, conversation prefix, optional thinking level, context counts and optional settled token rate. A ready context ceiling is labelled `max`, never used as a percentage denominator. Only a confirmed usage maximum enables percent; a later missing maximum clears it. Unknown seat/authority/plan/cache/task facts stay absent. The thinking action opens the verified selector; other footer actions remain inert, and closed sessions clear controls. This is the source-default ordering, not user-preference transport or complete footer parity.
- Reported reasoning now uses the existing dashed thinking-card geometry. Claude's additive `reconcile: append|replace` wire field keeps authoritative snapshots from duplicating a prefix; older ambiguous Claude reasoning fails visibly rather than guessing. Generic unannotated deltas remain append-only. Reasoning never substitutes for an answer or completion receipt. It shares answer-text limits; no timing/token counts are fabricated. Thinking fold controls, preferences and timing remain open.
- Native tool **display records** now retain per-turn IDs, input snapshots, nullable reported failure and first-seen transcript position between assistant cards. Explicit `card_index` ownership keeps late answer/reasoning corrections on their original card. Orphan results say the request is unavailable; pending tools become incomplete at turn end/closure. These observations never dispatch tools or grant approval. Tool folding/timing and live-provider tool verification remain open.
- Identified RPC approvals now have a separate live modal: **Deny** is initially selected; Tab/Left/Right selects **Allow once**, Enter sends one decision, Escape denies, and Ctrl+Q quits. Up/Down/PgUp/PgDn scroll the full escaped request. Typing, paste, clipboard chords and key repeats cannot grant permission. The prompt remains pending until a source closure event, not merely a command ACK. Concurrent requests retain their own IDs; stale intents never target the next prompt. Always-allow and approval mouse controls remain open.
- A source approval stop now leaves the interactive session ready for a new explicit prompt once all approval closures and decision ACKs settle. The stopped row retains its reason and partial reply across follow-ups and shutdown; it is never labelled completed. Late ACKs preserve that stop outcome. Refusal may arrive before a local request or without answer text. Reasons are capped at 16 KiB and share transcript/display budgets. One-shot mode still reports refusal as failure and cleans up.
- Structured source questions now open a separate form: Tab/Up/Down changes focus, Left/Right on the question tabs changes questions, Enter/Space selects, and notes support typing/paste. Single/multiple choices, free-text, masked secret notes, Clear, Submit, Chat about this and Cancel are supported. Submit requires all questions answered; Chat permits partial answers; Escape cancels without sending notes. Mouse press/release must target the same unchanged control. ACKs close answered forms; source cancellations and late replies remain correlated. Input never falls through to the saved chat draft. Ctrl+Q still quits cleanly. Requests are bounded to 8 questions/10 options each/16 KiB text, notes to 1,024 characters each and 16 KiB combined, and the session to 100 requests with 16 pending. No automatic answers or persistent question history are added.
- Tools can now be enabled explicitly with --tools on --workspace <absolute-directory> and --tool-profile strict|interactive|autonomous (explicit selection required); the footer reports actual source policy. External MCP remains off. Slash commands, remaining footer actions/preferences, saved theme selection, reconnect/resume, remaining management screens and later snapshot refresh are **not yet wired**. Unsupported runtime events fail visibly; no implicit approval or mock tool action occurs.
- This scoped view allows at most 100 rows / 8 MiB retained text, with a stricter independent **32,768 display-unit budget** (line breaks, escaped controls, wide/scalar text and card overhead are charged). Overflow fails visibly before expensive layout, never silently truncates. The composer accepts up to 16,384 characters; transport prompts have a 64 KiB ceiling. These temporary safety limits are not a full-parity claim.

**Evidence:** real Claude returned `ORANGE` on two sequential prompts, including recall from the first turn, then whole-job cleanup returned `Ok(())`: `C:/Projects/LiteTUI/output/tasks/t-af329ae5909646e59918061f69e447f8.log`. Automated TestBackend tests exercise live composer/rows/card geometry without fixture text; controlled child tests exercise serialized commands and error/quit cleanup. **Ryan's actual desktop hands-on acceptance is pending.** Desktop checks were stopped without clicks/keys when Ryan was gaming. Ryan subsequently clarified that visible test windows are allowed; do not send desktop clicks/keys or steal focus while he plays. The single idle visual-test runtime was stopped by exact owned PID, which is forced cleanup, not graceful-quit evidence.

**Approval-stop verification (2026-10-29):** 252 ordinary Rust tests and all 15 explicitly enabled controlled-process tests passed. The controlled child emitted two stopped turns (ACK before closure, then ACK after the single terminal event), remained alive beyond the previous two-second deadline, accepted a third explicit prompt and shut down cleanly. One-shot refusal without a local request returned failure with successful cleanup. Renderer regressions verify the persistent stopped label, escaped reason, partial reply, follow-up, shutdown and retained display budget. Cargo fmt, Clippy with warnings denied, build, CLI help and plan-document checks passed on Rust/Cargo 1.92.0. No real-provider test or desktop interaction was performed; the paid Claude smoke remained ignored in the ordinary suite. This is a continuation checkpoint, not full parity or tool-enabled acceptance.

**Approval checkpoint verification:** 245 ordinary Rust + 13 explicitly enabled controlled-process tests passed, plus 203 focused Python tests. Rust fmt/Clippy/build and focused Python mypy passed. Two unchanged `tool_approval.py` Ruff findings (`RUF012`, `BLE001`) remain; the other changed Python files pass Ruff. Expanded relay tests reproduced 13 failures and 3 teardown errors with the changed Python modules replaced in-process by their exact committed-HEAD versions, so those are recorded baseline failures, not a green suite. The controlled child proves explicit decisions, ACK/closure/late-response ordering and deadline/cleanup; it executes no tool. Actual Ratatui cells were rasterized and inspected, not exercised on Ryan's desktop. Requests are bounded to 100 per runtime / 16 pending / 16 KiB each; the configured turn deadline continues while waiting, even for an unbounded source wait. Expiry shuts down without inventing a denial. No real-provider retry occurred.

**Launch-profile verification (2026-10-29):** 293 ordinary Rust tests, 26 controlled-process tests and the controlled PowerShell launcher test passed, plus fmt/Clippy/build. Missing and cancelled selections fail at the runtime boundary before data creation or Python startup; a process tripwire stays untouched, while the explicit-profile success control starts it. The picker is tested at minimum size, across resize, repeats, paste, modifiers and mouse presses. Both launchers preserve absent selection and forward all three explicit profiles. No provider request or hands-on terminal acceptance was performed.

**Cancellation verification (2026-10-29):** 288 ordinary Rust tests + 24 controlled-process tests passed, with Rust fmt/Clippy/build. The real Python authority fixture cancels a pending write, then denies and approves separate writes with durable outcomes and cleared waiters. The affected Python run had 159 passes, 7 skips and one reproduced baseline test-runner failure; changed-source Ruff and lifecycle-fixture mypy findings match committed HEAD (six and three respectively, zero added). No real-provider or desktop cancellation acceptance was performed. Full parity remains open; the subsequent launch-profile checkpoint closes the earlier strict-default gap.

**Tool-mode verification:** 276 ordinary Rust + 22 controlled-process + 103 affected Python tests passed, with Rust fmt/Clippy/build and focused Python lint/type checks. A combined Rust/Python test denies then approves disposable writes through real source authority and owned persistence, with a controlled executor and no provider. Startup policy mismatches prevent prompt submission. app.py has 112 baseline Ruff findings and zero added. Real provider tool use and hands-on terminal acceptance remain unverified.

**Question verification:** 270 ordinary Rust + 19 controlled-process + 21 focused Python tests passed; Rust fmt/Clippy/build and new Python test lint/type checks passed. Source serialization/dispatch, three-turn answers, ACK ordering, cancellation, deadline expiry, secret masking, input ownership and compact note editing are covered. This is not real-provider question or hands-on terminal acceptance. Tools/MCP remain disabled.

**Native activity verification:** 222 ordinary Rust + 10 controlled-process + 114 affected Python tests passed; focused lint/type/build/docs checks passed. Native tool ordering/failure display is controlled-test evidence; tools are still disabled. An earlier real two-turn regression passed, but the latest rerun failed on turn two because the API could not confirm `claude-sonnet-5-5` (`unrecognized_model`), with runtime cleanup `Ok(())`: `output/tasks/t-2b883779eec1467e952d8284bdf5f743.log`. No fallback/retry; latest live verification is not green.

**Reasoning verification:** Python producer regression tests first failed for lost append/replace metadata, then passed after the additive fix. The affected Python set has 110 passes; focused Ruff/mypy are clean. Rust renderer and controlled-child tests cover replacement, isolation from answers, lifecycle refusal, escaping and shared limits. Real Claude math smoke (`output/tasks/t-4af3d61556b04f149298327239cbb2ee.log`) emitted empty reasoning deltas plus an empty replacement snapshot, then answered and exited cleanly. That proves live wire compatibility, **not nonempty reasoning or desktop visual acceptance**; nonempty replacement/render evidence is controlled.

**Footer verification:** 210 ordinary Rust tests + 10 controlled-process tests pass. A further real two-turn Claude smoke observed reported ready facts and measured context (`6,267 / 200,000` after turn two), with cleanup `Ok(())`: `C:/Projects/LiteTUI/output/tasks/t-80d844f0b563461ba1cd1e2cb989e367.log`. Renderer tests cover the actual footer path. Severity colours, footer actions/preferences and unreported facts remain open.

Direct syntax: `litetui-rs --interactive --python <exe> --source <repo> --data-root <NEW absolute directory> --backend claude --model claude-sonnet-5-5 [--timeout 180]`. It requires a real terminal; prompts come from the composer, not `--prompt`.

## Run the real one-prompt path

From PowerShell, one command (builds current source; requires the existing Python environment and Claude authentication):

```powershell
& 'C:/Projects/.worktrees/OpenBolt-rust-tui/rust/litetui-terminal/open-live.ps1'
```

Optionally pass `-Prompt 'Your short prompt'` or `-Python 'path/to/python.exe'`. This uses Claude `claude-sonnet-5-5`; no automatic fallback or retry. Each invocation creates a **new** `C:/Projects/.scratch/T0565/claude-live-<guid>` data root, retained for diagnosis. Its settings contain explicit isolated overrides layered on shipped defaults, never a copy of live settings. This launcher keeps tools and external MCP disabled by default. This is ordinary owned CLI startup, not a constructor/ownership bypass.

The direct binary interface is `--live --python <exe> --source <repo> --data-root <NEW absolute directory> --backend <claude|codex> --model <slug> --prompt <text> --tool-profile <strict|interactive|autonomous> [--timeout <1..600 seconds>]`. It also works with redirected stdout when the profile is supplied explicitly. Claude was verified; Codex is not live-verified by this milestone.

This one-shot path is **plain streaming terminal output**; use `--interactive` above for the Ratatui composer/transcript. It uses source-defined single-turn legacy RPC, not negotiated GUI events or invented execution identities. Ready must match the requested backend/model; ACK is not completion. Only `turn_end` with `stopReason: stop`, no error and received answer text succeeds. Unknown/interactivity events fail rather than receiving automatic approvals. Native snapshots are explicitly labelled as replacements. Control characters are escaped. Raw frames, channel chunks, stderr tail and emitted answer size have explicit bounds; the runtime has a deadline and graceful shutdown. Windows assigns the suspended child to a private kill-on-close Job before resuming it, and cleanup waits for the whole job. Stalled stdin/stdout cannot block the supervisor; stdout remains validated through shutdown/EOF. Forced termination is reported as failure. Non-Windows live startup refuses.

**Measured evidence:** one real Claude prompt returned `Hello`, followed by `turn_end: stop`, successful Python exit, and no remaining `RustLive` agent in a fresh fleet discovery. Smoke log: `C:/Projects/LiteTUI/output/tasks/t-24b0ef83c301422c9037191dd755d13b.log`. The child temporarily registered in the real fleet despite its isolated data root; **liteharness-home isolation remains a known gap**. Process exit alone is not a universal provider-descendant cleanup proof. No local model service was queried or started. Native tool views/approvals, full state reconciliation and the rest of the parity plan remain open.

## Open it on Windows

Double-click **`open-preview.cmd`** in this folder. It opens a separate, explicitly dark Windows Terminal window. If the executable is missing, the launcher builds it with Cargo first. Without Windows Terminal, it runs in the current console.

From a LiteSuite terminal pane (or any interactive terminal):

```powershell
cd C:\Projects\.worktrees\OpenBolt-rust-tui\rust\litetui-terminal
cargo build --locked
.\target\debug\litetui-rs.exe --preview
```

The binary is at `target/debug/litetui-rs.exe`. Build again after changing source; the double-click launcher only builds a missing binary. `--help` works without a terminal; redirected-pipe interactive launches fail with an actionable error.

## What you can inspect

- A mock user/assistant exchange and a short illustrative reasoning summary.
- Completed tool calls; expanded arguments/results; a simulated running call; a failed-call example.
- A mock write approval that defaults to **Deny**. Both choices only change local fixture text.
- Scrolling, selection, expansion, resize behavior, and a composer that adds local demo turns.

| Input | Action |
| --- | --- |
| Tab / Shift+Tab (completion closed) | Select next / previous tool |
| F2 | Expand / collapse selected tool |
| Ctrl+B | Previous tool |
| PgUp / PgDn, mouse wheel | Scroll transcript |
| Ctrl+Home / Ctrl+End | Start / end of transcript |
| Left / Right, Home / End | Move composer cursor |
| Shift+Left / Right / Home / End | Select composer text |
| Ctrl+Left / Right (+Shift to select) | Move to previous / next word start |
| Ctrl+W / Ctrl+F | Delete to previous / next word start (selection first) |
| Ctrl+A / Ctrl+E | Composer start / end (focused-input bindings) |
| Ctrl+Shift+A | Select all composer text |
| Up / Down (completion closed) | Recall prompt history / restore original draft; another Down enters the footer |
| Left / Right, Enter, Up / Escape (footer) | Cycle visible chips, report inert request, leave footer |
| Type `/pre` or `/` | Filter the three mock command/skill completion entries |
| Up / Down, Tab, Escape (completion) | Select, replace draft with `/name `, dismiss |
| Left-click a completion row | Accept that name without submitting |
| F4 | Open searchable public dark-theme picker |
| Up / Down, Enter, Escape (picker) | Select, apply in memory, cancel |
| F6 | Open the pending mock approval |
| Tab then Enter, or A / D | Choose an action inside the mock approval |
| Escape | Close the mock approval without deciding |
| F5 | Reset/replay the fixture conversation |
| Enter | Add composer text as a local mock turn |
| Backspace / Delete (or Ctrl+D) | Delete selected text or adjacent character |
| Ctrl+U / Ctrl+K | Delete from cursor to start / end |
| Ctrl+C | Copy selected composer text via terminal OSC52; otherwise show quit help |
| Ctrl+V | Read Windows clipboard text and insert at active cursor (not selection replacement) |
| Ctrl+Q | Exit and restore terminal modes |

Below 36 columns or 14 rows, the preview asks for a resize and suspends decision keys; a hidden approval cannot be confirmed. No full editor, mouse text selection, markdown renderer, full clipboard/host parity, backend selector, settings screen, image support or runtime transport is claimed yet.

The composer edits at Unicode scalar boundaries and replaces selections on insertion. Rendering uses whole graphemes: an internal scalar caret position is displayed at the glyph's trailing edge, and horizontal scrolling preserves complete wide glyphs. A single grapheme wider than the input viewport is displayed as � until it fits again; the underlying text and scalar selection remain intact. History preserves the original draft while browsing and restores it at the end; edits of recalled entries do not change stored history. Preview-only limits are 500 characters per draft and 100 history entries, not production defaults. Ordinary insertion converts CR/LF/tab to spaces and discards other controls. Bracketed paste instead takes only the first Python-style splitlines line and replaces the selection: an empty first line removes selected text, while an empty paste event does nothing. This event ownership was measured with a mounted source PromptInput: it stops the event before the application paste handler. Preview bounds and control filtering still apply, so unrestricted source-input parity is not claimed. The distinct Ctrl+V path normalizes CRLF/CR and joins nonblank lines, then inserts at the active cursor without replacing selected text. Its native Windows read is an explicit user-triggered boundary, not a side effect of bracketed paste or typing.

Confirmation requires a key press; reported repeat events cannot confirm a modal or submit a draft. Repeats of a modal-closing letter are suppressed until release or a fresh press, while ordinary typing and cursor repeats still work. Hosts reporting repeated physical keys as fresh presses cannot provide this distinction. Live clipboard-host verification, runtime completion catalog/dispatch and runtime submission remain open.

Word movement/deletion follows Textual 8.1.0's `(?<=\W)\w` searches on the cursor prefix/suffix, including punctuation and selection behavior. Classification is frozen to **Python 3.11.9 / Unicode 14.0.0**, the inspected source environment—not Rust's broader/current `is_alphanumeric`, and not a guarantee for future Python runtimes. `scripts/export_rust_word_boundaries.py --check` detects stale tables/oracles. Regenerate with the supported source interpreter only; an interpreter/Unicode upgrade changes this compatibility target and needs a reviewed fixture update. The Python tests compare the oracle against actual mounted Textual Input actions; Rust tests replay every fixture cursor with both selection orientations. No runtime Unicode-version negotiation exists yet.

Slash completion is non-modal: ordinary editing keeps focus in the composer, Up/Down selects before history, Tab accepts the entire name plus a trailing space, and Escape stays dismissed until the text changes. Enter while the composer is focused still submits the typed draft, not the highlighted completion. Prefix matches rank above substring matches; producer ordering is preserved within each group, results are capped at 200, and navigation clamps rather than wraps. The list rises above the composer with theme-primary borders and explicit command/skill labels; compact widths shorten the name column to retain the label. Hidden/covered lists cannot consume their acceptance keys.

The demo catalog contains only `preview-help`, `preview-replay`, and `preview-skill`; **none dispatches a command or runs a skill**. Real candidate discovery, alias/collision deduplication, live catalog refresh and picker-specific mouse-wheel scrolling are not implemented. Left-click acceptance uses the rendered row offset, requires press/release on the same row, and is invalidated by refiltering, hiding or viewport changes. Unicode case-insensitive filtering uses the Rust compiler's lowercase tables, so cross-version Python Unicode parity is not established here (unlike the separately frozen word-editing class). The catalog input contract requires source-ordered candidates; mock tests do not establish the producer contract.

## Footer projection and keyboard ownership

The footer now has independent status and permission rows, supplied from fixture fields rather than runtime state. Status spacing compacts before dropping throughput, background tasks, agents, cache, model, conversation and context, in that order. Seat truncation preserves whole graphemes and a minimum useful prefix; thinking is conceded next. If protected contents still cannot fit, the row shows a nonactionable ellipsis. Width accounting follows Ratatui's grapheme rendering, including Arabic strings whose whole-string Unicode width is smaller. A reported source authority must be explicitly supplied; the demo supplies none and never invents a registered seat or permission level. `plan:off` is only a fixture, not an authoritative runtime reading.

Navigation retains the source's **global configured order**, filtered to controls that actually render. It does not introduce row-major ordering. Left/Right wraps; Enter emits only an offline request notice and never changes a profile, plan state, thinking setting or saved preference. Up/Escape returns ownership to input. Other editing keys and paste leave footer mode, preserving the draft and caret. Completion has priority. A history Down first restores the draft; a subsequent fresh Down enters the footer. Down repeats cannot toggle ownership, and modified Enter cannot leak through to draft submission while the footer owns it.

Invisible-control filtering and nonactionable overflow deliberately tighten source behavior: the source navigation list can contain width-dropped or unresolved controls. If a selected chip disappears, ownership is canceled without choosing a replacement, and the next Enter is consumed rather than submitting the preserved draft. Other explicit input resumes ordinary editing. Theme/approval/quit help has moved to the composer border so the two footer rows can display status. Meters/toggle, rich per-span status colors, mouse footer interaction, settings normalization/visibility producers and real runtime actions remain open. TestBackend checks are automated rendering evidence, not live terminal or visual acceptance; the preview remains closed.

## Clipboard boundary and verification limits

Ctrl+C sends the selected input as an OSC52 base64 sequence, matching Textual's terminal-copy mechanism. It no longer quits; with no selection it explains Ctrl+Q. Sending the sequence successfully is **not proof the terminal copied it**. Ctrl+V reads Windows Unicode clipboard text through `clipboard-win`, checks the Windows allocation against an explicit **1 MiB preview limit before conversion**, and releases the clipboard guard on every outcome. Missing text and errors are reported without altering the draft; hidden inputs and preview modals do not call the clipboard boundary. Clipboard operations occur only in response to those keys, never on startup. Non-Windows OS reads currently report unsupported; there is no subprocess fallback.

The native implementation is compiled, **not verified against the live clipboard or terminal host**. Tests replace the boundary and check source input routing/normalization, error handling and OSC52 encoding; no system clipboard was read or changed during verification. Textual's internal clipboard cache, transcript-selection copying, image clipboard actions and general cross-platform clipboard support remain open. Ctrl+X is not repurposed as cut: the source application's priority binding reserves it for clearing an image attachment, which this preview does not implement.

## Safety and evidence boundary

No Python child, model request, tool execution, real approval, fleet registration or application-data persistence exists in this preview. Named paths, commands, test counts and results in the conversation are **fixtures**, not work actually performed. Typed messages are kept only in memory (six turns maximum); quitting discards them unless the user explicitly copies selected text to the terminal clipboard. Clipboard keys are the preview's only intentional user-data I/O boundary. Cargo/launcher build actions are separate developer operations, not mock tool execution.

The terminal defaults to the source-derived **Ash** palette: borderless amber tool rows on black; rounded gray assistant frames; nested dashed thinking blocks. Ryan accepted this revised visual direction on 2026-10-15 after rejecting the initial generic card redesign. Preserve the layout while wiring runtime events. F4 now offers **36 public dark themes**, generated from LiteTUI and Textual definitions without reading live settings. Search by name, use arrows, then Enter to apply; Escape cancels. F5 replays the fixture without resetting the theme. Semantic tokens drive the same layout across palettes, including resolved thinking/tool/footer overrides. Legacy fixed argument/result grays remain intentional. Selection is in-memory only: real custom-theme editing, runtime catalog loading and saved selection are still required. Synthetic override tests are not custom-theme integration. The terminal uses an explicit dark palette. A cleanup guard restores raw mode, mouse capture, bracketed paste, alternate screen and cursor on normal exit or recoverable errors. Panic handling attempts restoration. Force termination cannot guarantee cleanup.

## Offline wire primitives (not connected to the preview)

The library's `protocol` module provides an incremental JSONL decoder and a connection-local negotiation/correlation client. Neither starts a process, writes a command, applies application state, resolves an approval, or authorizes a mutation.

- The decoder requires an explicit positive byte limit per frame (LF excluded; CR included). It preserves complete records delivered before a later error and stops permanently on framing failure or EOF. JSON nesting is bounded at 128 values. Raw-aware container parsing preserves opaque objects and exact numeric payloads, including integers beyond machine range; it does not turn serde's private number-map spelling into a numeric identity.
- A matching successful version-1 hello establishes compatibility only. State arriving during negotiation latches uncertainty that a later hello cannot clear. Compatibility is not snapshot continuity or authority.
- A caller-supplied, reviewed delivery policy selects mirrored envelopes or direct-only events. Unknown events and malformed envelopes fail closed; equal consecutive events are never deduplicated. Event payload schemas and the production delivery inventory are **not implemented** by this module.
- Generated string command IDs are unique within a caller-owned generation. Incoming IDs preserve string/integer distinctions; unsupported numeric IDs outside i64/u64 range fail explicitly. Pending correlation is bounded. Faulted clients can drain valid pending responses; disconnected clients retain uncertain requests and never resend.

Tests cover these primitives with Rust fixtures and now replay **nine Python-produced synthetic output streams / 31 records**. The output-only text imports reconstruct LF framing and must match the original capture SHA-256 pins before replay; no JSON reserialization or ID rebinding occurs. Every two-part byte split and one-byte chunk stream is checked. Refusal-before-ACK, repeated event pairs, exact IDs and explicit nulls survive. Literal captured hello IDs remain unmatched against generated Rust requests: these tests do not pretend negotiation succeeded. See `tests/fixtures/wire/README.md` for provenance and limits. The original inputs/manifests/log/index are not packaged as a complete evidence bundle, and successful correlated negotiation still uses separate synthetic Rust tests. Dev-only SHA-256 verification uses pinned sha2 0.10.9. These wire primitives supply no production limits, asynchronous delivery owner, child lifecycle, general runtime reducer or live activation path; the separate bounded raw-ingress foundation is described below. The preview remains offline.

## Offline identified-stream projection (not connected to the preview)

The separate `stream` module projects explicitly attributed generic `turn_start`, `text_delta`, `reasoning_delta` and `turn_end` envelopes. A projection belongs to one connection generation and uses a canonical UUID4 execution key, retaining its original session/operation/hook/source separately. It requires a selected successful hello notice; raw mirrors and stale-generation notices cannot append text. Unknown operation/hook/source values remain null rather than borrowing another row's identity. Equal selected deltas append twice—there is no deduplication or replay policy. Each row aggregates one execution's answer/reasoning lanes; message/round boundaries are not invented when the wire supplies none.

The module validates the selected event/envelope pairing, identity and participating payload fields before mutation. Missing attribution/start, duplicate start, metadata drift, post-end events, malformed payloads, unsupported producers and uncertain transport latch a fault. Active rows then remain visibly incomplete; observed ended rows remain evidence of an end record, not successful operation, persistence, tool settlement or completion of queued input. Disconnect never manufactures a terminal record. Construction requires explicit positive row, retained UTF-8 text and metadata budgets; overflow preserves existing content and blocks rather than dropping/coalescing it. These are retained-content limits, not exact allocator/heap bounds or approved production defaults.

`ui::draw_stream_projection` renders those rows with the existing assistant frame, nested dashed thinking block and selected semantic theme. It displays supplied answer/reasoning instead of demo responses or fabricated timings. Terminal and bidi controls are escaped only for display; stored text remains unchanged. Wrapping/clipping preserves graphemes, and bounded-viewport scrolling uses the wrapped line count. “Stream ended” is deliberately not “operation succeeded.”

The module also projects `generic_tool_requested` / `generic_tool_returned` from the generic Python executor boundary. A distinct canonical UUID4 observation ID joins each pair; provider IDs and round/slot coordinates are metadata, never fallback identities. Duplicate requests (including across executions), missing/cross-execution returns and repeated returns block. Requests are retained in arrival order within their owning execution. `Requested` is not proof of authorization/dispatch; `Returned { result, ok }` retains the literal host flag and does not mean semantic success or background completion. A stream end, global fault or disconnect marks still-pending observations `Incomplete`, retaining already-returned data.

`Limits` additionally requires a positive aggregate `tool_calls` bound. Returned observations still count toward it. Tool identity/name/provider strings and two eight-byte coordinates share the metadata budget; results share the UTF-8 text budget with answer/reasoning. Full schema validation and budget checks precede insertion/return mutation. `round` and `slot` accept only nonnegative integer values representable as u64; out-of-range values fail rather than being rounded or inferred.

The renderer shows unbordered, themed tool groups **after each aggregate assistant row**, explicitly labeled “not transcript order.” It shares the demo's tool-body geometry without copying fixture arguments, timing, approval state or completion claims. Names, provider IDs and results use the same display-only control/bidi escaping. These rows have no collapse or execution controls yet; a real view controller and chronological message/round schema remain open.

The focused tests use synthetic matched negotiation through decoder → wire client → projection → Ratatui TestBackend rendering, plus interleaved origins, schema/correlation/lifecycle/budget failures, theme/resize and control-character checks. Historical captures remain unchanged. The six-event test policy is **not a production delivery inventory**: legacy tool events, native producers, usage and other events remain unsupported and block, not silently ignored. This library slice does not create a runtime process, wire the preview to events, implement general transcript reconstruction/Markdown, or establish live terminal visual acceptance.

## Bounded raw ingress (offline transport foundation)

`transport::Inbound` retains a FIFO of complete original JSONL frames plus one partial frame for a caller-owned connection generation. Positive caller limits bound frame bytes (excluding LF, including CR), queued frame count, and aggregate queued bytes (including LF). The partial buffer has its own frame bound. Popping a frame transfers ownership and releases its queue charge. These are logical retained-content bounds, not allocator/heap limits; borrowed input, Vec capacity and frames retained by the consumer are outside them. No production defaults are supplied.

Queue exhaustion is **fail-stop overload rejection, not backpressure**. Capacity is checked per frame before enqueueing. On overflow the offending frame and current input suffix are discarded, the first fault latches permanently, and the previously accepted FIFO prefix remains drainable. Later input/EOF calls cannot reset it; a new connection needs a fresh caller generation. Stale-generation calls are inert. Chunk/drain scheduling affects overload: a large coalesced feed can fill the queue where smaller feeds with intervening drains would not.

Blank complete lines follow the existing decoder's ASCII-whitespace convention exactly (including form feed, excluding vertical tab); oversized whitespace lines still fault. Raw nonblank bytes—including invalid JSON/UTF-8—are left untouched for the decoder to validate. EOF with any partial bytes, including whitespace, faults as truncated; an empty partial buffer records EOF, not successful operation completion.

**Drain accepted frames before delivering terminal uncertainty.** A valid frame boundary at the decoder does not erase an ingress failure. The caller must separately propagate EOF/fault to its wire client/projection and prohibit new work as soon as ingress is terminal. A previously accepted terminal record remains observed even if later bytes overflow; this inbox cannot retroactively turn it into an unobserved record. Transport-accepted frames are not necessarily decoder-valid: on a decoder failure, stop protocol delivery and only drain/discard the remaining raw frames for cleanup; never replay them through a replacement decoder. Tests exercise this order through the actual decoder/client/projection. This module does not yet implement the session owner, async pipe readers, writer, stderr drain, cancellation, child/descendant cleanup or live connection.

## Dependencies and Ratty target

Pinned Ratatui **0.30.2**, Crossterm **0.29.0**, unicode-width **0.2.2**, unicode-segmentation **1.13.3**, serde **1.0.229**, serde_json **1.0.151** (`arbitrary_precision` and `raw_value`), base64 **0.22.1**, and Windows-only clipboard-win **5.4.1**; lockfile committed. Rust 1.92.0 was used to build. The package declares the dependencies' supported Rust floor (1.88), but that older compiler was not tested here.

On 2026-10-15 the upstream Ratty `widget/Cargo.toml` identified the widget as **`ratatui-ratty` 0.4.0**, not the earlier `ratatui-rgp` name. It depends on `ratatui-core = 0.1.2` and uses Ratatui 0.30 / Crossterm 0.29 in examples. This preview resolves the same core line. That is a dependency compatibility observation, **not a tested RGP/inline-3D integration**. No Ratty/GPU dependencies are added.

Source: https://github.com/orhun/ratty/blob/main/widget/Cargo.toml (moving upstream reference; re-check before later integration).

## Focused verification

```powershell
cargo test --locked
cargo fmt --check
cargo clippy --all-targets --locked -- -D warnings
cargo build --locked
```

Tests cover all public palettes, picker search/navigation/input ownership, synthetic custom overrides, narrow layouts, fixture state, mock decisions, modal input isolation, input bounds/control-character filtering, cursor/selection/history editing, grapheme-safe caret geometry, modal confirmation repeats, scrolling, explicit dark cells, compact dialog visibility, and actual `TestBackend` resize sequences. They do not establish real-runtime or full terminal-host parity.


The full controlled-process suite now also requires a Python environment with LiteTUI dependencies for its real authority/store fixture. Set LITETUI_TEST_PYTHON to Python 3.11 and LITETUI_AUTHORITY_PYTHON to the existing LiteTUI environment, then run only the live_process test target with --ignored. The separate live_claude target remains a paid opt-in smoke and is not included in this command.
