# LiteTUI Rust — interactive live chat and offline preview

**Interactive text chat is implemented; full parity is not complete.** `--interactive` connects the existing composer and transcript to one real owned Python runtime for multiple turns. `--live` remains the one-prompt streaming smoke path. Default / `--preview` remains an explicitly offline mock conversation.

## Runtime management connection

Live startup requires matching protocol/data versions, advertised hello/state operations, and a valid idle snapshot whose session, backend, model and profile match readiness. This read does not grant authority or prove persistence, and a fresh pending store may report writable false. Only bounded startup facts are retained; the snapshot's raw settings are not displayed or retained in view state.

After negotiation, each source raw event is paired with its GUI envelope and only the envelope reaches the transcript. Equal events remain separate occurrences. Pair tracking accepts cross-thread interleaving, caps unmatched records at 64 / 4 MiB serialized payload, rejects unmatched EOF/new prompt, and applies a deadline no longer than twenty seconds even while chat is idle. This supplements the existing frame, pipe, transcript and display-work limits.

Native and generic turn producers must carry the captured execution/session/operation/hook/source origin. Late or changed origins fail before content reaches another turn. Approval/question closures retain their separate request ledgers; their ambient operation may be the cancellation command. Model changes outside an owned management operation remain unsupported. Startup snapshots never settle turns or overwrite newer stream facts. Model/thinking controls are described below; later snapshot refresh and the remaining management screens are still pending.

## Reopen saved conversations

After interactive exit, the terminal prints the retained root, full conversation ID and current backend/model. Resume that saved conversation explicitly:

    ./open-chat.ps1 -DataRoot 'C:/path/to/retained-root' -Resume '<conversation-uuid>' -Backend claude -Model '<saved-model>'

For a root with tools enabled, also pass -Tools on -Workspace '<absolute-directory>'. Select the requested tool profile at launch or provide -ToolProfile. The direct binary accepts --resume <conversation-uuid> with the same --data-root/--backend/--model/options. The Rust client never resubmits history as a prompt. One-shot mode requires a new explicit --prompt. Rust requests Python's hold-restored-input policy: recovered Codex entries stay held across restart and later explicit turns without changing their saved delivery evidence. Resume verifies that policy; the notice and held:N footer show the held count. F7 inspects held input and can explicitly cancel or continue eligible entries. F7 also checks provider history for uncertain delivery; unproven input remains held.

Fresh starts still require a new root. Resume requires the versioned rust-frontend.json marker created by this version, matching saved tools mode and external MCP off. It reads existing launch settings without overwriting them. Roots predating this marker and legacy archives are not automatically adopted or migrated. Python remains the authority for ownership locks, exact conversation lookup and saved execution settings; mismatched backend/model flags refuse rather than changing the saved agent. Saved thinking is preserved.

Bounded saved history: interactive launches negotiate the bounded history protocol before startup replay. The composer opens only after independent owned state and whole-context history proofs succeed. F6 opens Saved History for the current conversation: arrows/PageUp/PageDown scroll or change pages, Home/End select the first/last page, F5 refreshes, and Escape closes. The inspector retains one inert page separately from live rows, unsaved partial output and the composer draft. A changed revision keeps the old page visibly stale until explicit refresh. Full source context remains available to the next prompt, including hidden system and provider continuity fields. Saved reasoning, native activity, plans and summaries have typed blocks; images and saved questions use inert notices. Generic tool results remain in model context with an omission count. No historical tools or questions become active. The old 100-visible/1,000-total message ceiling remains only in the legacy protocol path; version 1 instead bounds raw/semantic history at 8 MiB, display projection at 16 MiB, packed offsets at 1 MiB and each complete response at 1 MiB. F8 also inspects any parsed saved row without changing the active conversation; explicit Open remains a separate review.

**Verification (2026-10-29):** 327 ordinary Rust tests, 33 controlled-process tests and two launcher tests passed. The real-source fixture executes Python's actual CLI/acquire/resume/store path across two process lifetimes, verifies prior context and saved thinking, rejects a competing owner/missing conversation/wrong model, and proves no duplicate submission. A withheld transcript response times out and cleans up. Provider, hardware and visual boundaries are controlled. Rust fmt/Clippy/build and fixture Ruff/mypy pass. Existing unchanged Python resume tests report 14 passes and 31 ownership-fixture baseline failures. Real-provider and hands-on terminal acceptance remain pending.

## Browse owned saved conversations

In idle live chat, **F8** opens the current agent's saved conversations. Up/Down or paired clicks select a row; PageUp/PageDown request the previous/next source page. Left/Right inspect long metadata, R refreshes, and Escape closes. **I** opens an inert history inspector for the selected row, including read-only or incompatible-to-open conversations. **O**, then a fresh **Enter** on the visible full-UUID confirmation, opens the selected conversation. The draft is preserved. A hidden, resized or changed selection cannot confirm.

Python validates a fresh catalog snapshot and provides a bounded read-only preview. Rust applies its actual history/display parser before issuing Open; an oversized preview leaves the old session usable. After opening, Rust waits for source connection and native-history reconciliation, then independently reads current ownership, effective tool policy and held input before switching the transcript. Failed or contradictory opening/readback closes the connection with an uncertainty diagnostic; it does not claim the old source session was restored. Opening never submits the draft or releases held provider input.

Inspection preserves the active conversation, complete provider context, settings, held input and composer draft. F5 finds the exact saved UUID in a fresh catalog before rereading its history; deletion or revision drift keeps the previous page visibly stale until a successful explicit refresh. Escape returns to the same browser selection after any in-flight read settles. The underlying catalog is unchanged and may need its own R refresh before Open. Inspection never executes hooks, connects a provider or authorizes Open. F6 always targets the current conversation.

The current browser lists only the owned agent, 50 entries per page, up to 10,000 directory entries. Catalogue reads retain their 8 MiB per-transcript and 32 MiB per-page limits, with 256 KiB catalogue responses. Read-only, incompatible-policy and unsupported-history rows show their reason and cannot open. Version-1 history uses the bounded descriptor/page contract above; the old message-count limits apply only to the legacy protocol. F8 New and Rename use separate guarded reviews. Current host-history edit/retry and reviewed manual compaction are available through F6 as described below. Provider-native compaction and rewind remain parity work.

**Saved-row inspection verification (2026-10-31):** 580 ordinary Rust tests, 111 focused catalog tests, 17 controlled source cases and 18 native inspection scenarios pass. The relocated clean-dependency package passes all 17 source cases and 12 selected native scenarios; all 346 files remain unchanged. Formatting, type checks, Clippy and build pass. These checks do not establish actual-provider or terminal-host acceptance.

## Review and edit or retry a current user message

Open F6 on the current conversation, use Tab/Shift+Tab to select a user message, then E to edit or R to retry. Edit has its own lossless text buffer; Retry uses the complete original source text. Read every review page and press Enter on the last visible page to send once. Escape abandons an unsent review. Your ordinary composer text, caret and selection stay unchanged.

The review identifies the retained prefix and discarded suffix. A successful action replaces model context from that user message onward; original transcript records remain on disk and earlier tool effects are not undone. Source admission, policy and prompt hooks run before durable publication, followed by independent persistence and native history readback. Old live rows remain until readback succeeds; new answer, compaction and continuation rows are retained separately. Stop remains available during execution, and Quit joins the owned work.

Only bounded plain-text messages in the current owned writable host-history conversation are eligible. Saved-row inspection, images, slash input and Codex/Claude native history cannot authorize this action. Hooks needing a new prewrite approval refuse without publication; ordinary in-turn approvals and questions remain available. Captured background work and automatic promotion retain their exact input, worker and continuation identities through retirement. Existing automatic/self-compaction and their source-owned wake continuations preserve hook suppression. Provider-native rewind remains open.

## Review manual compaction

Open fresh Current history with F6, then press C. No user message needs to be selected. Enter an optional hint, review the source-selected older/recent counts and settings, then confirm on the final visible page. Escape returns from review to the preserved hint; Escape again closes the editor. The normal draft, caret and selection stay unchanged.

The review shows tool execution, possible durable file writes, wake, screen-clearing and compaction budgets. The canonical source retains complete tool pairs, injects current memory and summarizes older context. Compaction may grow context; reported post-compaction token counts are estimates. Completed tool writes survive Stop, a failed summary or a provider failure.

The action first runs maintenance, with no invented user prompt. If enabled, a real wake follows. Approvals and questions remain available, and Stop applies until the action settles. Source and native readback distinguish compacted, no change, cancelled and failed outcomes before another prompt is allowed. Only successful compaction applies the reviewed clear-screen setting; retained older scrollback is marked separately from current context. Quit joins owned work without claiming the interrupted effects succeeded.

This slice supports current owned writable host-managed history. Provider-native context and owner-mode handoff remain unavailable. Its latest validation status is in `../../docs/Plans/rust-tui-port/master.md`; real-provider and terminal-host acceptance remain separate.

## Review compaction preferences

F10 → C opens the compaction preference editor; E on a compaction row opens that field. Tab/Up/Down select a preference. Edit numeric values directly; Left/Right change thinking or boolean choices. Enter requests a source review. Read every page, then confirm on the final visible page. F5 refreshes without discarding drafts. PageUp/PageDown scroll the draft details, including complete read-only reasons on small terminals; return to the first page to edit. Escape returns from review to the draft, then to the F10 inspector.

Keep-recent, summary tokens, tool rounds, thinking and wake save with the conversation. Clear-screen saves for the device and affects later successful compaction. Saving does not compact, clear history, wake or contact a provider. Tool enablement is shown read-only. Keep-recent preserves complete tool boundaries; resolved thinking may be lower than the saved preference. The summary token budget also caps other summary, goal-evaluation and subagent calls.

A saved or partial result is shown only after independent source and runtime readback. The ordinary composer remains unchanged. Current verification status is in the port master; this work does not establish live-provider or terminal-host acceptance.

## Work inspection (in progress)

F1 opens Work during ordinary chat. Tab/Shift+Tab switch between host tasks, provider activity, scheduled jobs, loops, the current goal and the calendar. Enter opens complete details; T reads a selected finished host task's bounded output suffix. N/PageDown and B/PageUp follow actual source page offsets, including pages shortened to fit their byte budget. Calendar Left/Right changes month and Enter opens the selected day's complete schedule entries. F5 refreshes the captured facts.

Unavailable sources are labelled separately from empty lists. Host task ownership is descriptive, not permission to kill a process. Task output uses the canonical log path and reports omitted bytes and replacement decoding. These inspection operations preserve the ordinary composer draft, caret and selection; approvals, questions and Stop take priority. Closing a dispatched request waits for its correlated reply before releasing the composer.

P reviews pausing future scheduled prompts for this runtime. Confirmation requires reading every page and a fresh Enter on the final visible page. Success waits for source readback; active tasks, goals, pending input and saved job definitions remain unchanged. Work uses its own request deadline while chat continues.

K on a selected owned running host task opens its full source Stop review. After confirmation, Stopped appears only after process, pipes, worker, callback and durable task retirement are independently verified. Final task output may still continue the conversation. F5 refreshes the invalidated task capture.

The native launcher requests captured background lifetimes and waits for activation before admitting autonomous input. Rust preserves exact payload/profile display, source/local-prompt races and verified session rebind. Committed child results use receipt-bound status rows without duplicate user input. F7 can reconcile an uncertain input against positive provider history; the selected source readback must finish before that input releases admission, and other uncertainty remains held. Automatic cron/loop jobs that open a new conversation now propose a captured handoff. Rust accepts only while the editor, local submission and owned work are idle, verifies the exact durable stamp and new conversation, then recaptures Work before the one owned invocation may continue. A local unsent draft is preserved. Manual Run remains separate parity work. Current evidence is recorded in the port master; source-process, package, terminal-host and real-provider acceptance remain separate.

Within F1, Tab/Shift+Tab selects tasks, provider activity, jobs, loops, goal, calendar or Recovery. C creates a job or loop; E edits the selected configuration; D reviews deletion. Space enables or pauses the selected loop, with canonical timer rearming. On Goal, P pauses and D clears the saved goal. Configuration saves never start work; an enabled job may run on a later normal scheduler tick. Explicit job Run and goal start/resume remain unavailable in this slice.

Forms preserve multiline text. Tab/Shift+Tab changes fields, Space toggles a boolean, Ctrl+U clears a field, Shift+Enter adds a newline and Enter requests review. Paste targets only the visible editable field. Every review page must be displayed before a fresh Enter on the final page can apply once. Independent source readback verifies the selected saved configuration and separately checks any execution counter advancement. F5 refreshes Work before another change.

Recovery shows a retained staged invocation after an interrupted scheduled handoff. D reviews discarding its hold. Confirmation preserves the original record and input identity in a terminal audit, then independently reads back the discard and a fresh Work snapshot. It never runs the job again, rewinds its stamp, claims earlier effects were undone or resolves unrelated input uncertainty. A live invocation cannot be discarded. Closing drains owned operations and their shutdown receipts without starting a new conversation or publishing a successful action.


## Held input

In idle live chat, including when held input is the only uncertainty, **F7** opens recovered input. Up/Down selects an entry; PageUp/PageDown or the wheel scrolls its complete text and delivery details. **Delete**, followed by a fresh **Enter** on the confirmation, cancels an eligible entry. **C**, then a fresh **Enter**, continues the oldest eligible input offered by Python. Its saved source/profile is preserved and the composer draft is untouched. Escape keeps it; R refreshes. Only queued or explicitly rejected next-turn Codex entries and prepared Claude entries can be cancelled. **H**, then a fresh **Enter**, checks eligible uncertain entries against provider history. A missing match leaves them held; only exact positive acceptance can record them locally.

Cancellation is verified in Python's saved journal and then read back by Rust before the footer changes or another prompt can be submitted. Failed persistence/readback never triggers an automatic retry. Source changes, resizing, navigation and pointer activity disarm confirmation. The draft and clipboard remain isolated while the inspector owns input. Continuation admission is separate from execution: a pre-start refusal is labelled Not started. After refusal or turn settlement, submission stays blocked until Python confirms the exact worker has retired and returns the saved queue. History recovery never resends input or creates a new turn. Its verified transcript row says Previously accepted. Entries without positive evidence remain held. Prepared Claude entries can be continued explicitly; uncertainty never offers a replay action.

**Continuation verification (2026-10-29):** 351 ordinary Rust and 130 focused Python tests passed, with Rust fmt/Clippy/build/help and new source/tests/fixture Ruff/mypy clean. GUI diagnostics match the existing five Ruff/eight mypy findings. The owned-source process test verifies refusal, explicit retry, one saved materialization and worker-retirement readback before a follow-up. All 36 controlled-process tests passed. Providers and desktop interaction remain controlled; full parity is not complete.

Claude saved entries also appear here after resume, including native session, segment, workspace and uncertainty origin. Submitted/acknowledged entries remain uncertain and held. A prepared entry can be cancelled with durable verification. **X**, then a fresh **Enter**, explicitly resolves one uncertain Claude delivery without replay; its provider effects remain unverified. The source saves that decision before Rust reads the queue again. A prepared Claude entry supports C then a fresh Enter, preserving its original saved user record. A cache refusal before execution stays prepared; a failed submitted turn stays uncertain until independently resolved. New attempts reserve fresh operation IDs across restarts.

**Claude resolution verification (2026-10-29):** 361 ordinary Rust tests and 111 focused Python cases pass. The controlled owned CLI restart test verifies selected-only durable resolution, no execution row or replay, preserved other holds and clean shutdown. Current shared-process evidence is in the latest plan checkpoint. Provider and host acceptance remain open.

**Claude recovery verification (2026-10-29):** 358 ordinary Rust tests pass, and 176 focused Python cases pass across the combined run plus the documented five-case wording-fix rerun. The new controlled owned CLI restart/cancellation scenario passes. Full affected process evidence and baseline lint/type counts are recorded in the latest plan checkpoint. No provider or desktop acceptance is implied.

**History-recovery verification (2026-10-29):** 356 ordinary Rust tests and 137 focused Python cases pass. The controlled owned-source subprocess verifies absent then positive paginated history, one saved user record, no provider send, and follow-up context. Final process-suite evidence is in the latest plan checkpoint. New code/fixtures pass Ruff/mypy; GUI diagnostics and the legacy steering test's 21 mypy findings are unchanged. Actual-provider and terminal-host acceptance remain open.

## Native model and thinking selection

In idle live chat, **F3** opens models; **Tab** switches between model and thinking choices. The thinking footer opens thinking choices directly. Up/Down or a paired left-click selects a row; a fresh Enter applies it. Left/Right inspect long values and source explanations. R refreshes; Escape closes. The saved draft, paste and clipboard remain isolated. Choices cannot be confirmed while hidden, waiting, resized away, or held from an earlier key press.

The connected runtime supplies the catalogue, allowed thinking levels and current selection. Each apply is followed by a separate readback before another prompt can be admitted. Failed requests show the actual state read back; an uncertain readback stops the connection. There is no automatic retry or rollback. Closing after a request was sent does not cancel it or release the composer early. A verified model change clears the previous model's context/rate/thinking facts until new facts arrive. F9 backend switching and F12 engine controls are described below.

The controls cap retained metadata at 64 KiB, model rows at 512 and thinking rows at 32. Source values are escaped only for rendering. The default thinking choice means the Python runtime's default selection; reported capability levels do not prove provider support or an inference effect.

**Verification (2026-10-29):** 319 ordinary Rust tests and 31 controlled-process tests passed, with fmt/Clippy/build/CLI help. The real-source controlled process verifies setters, owned persistence, failed-write readback and the next turn's selected values without contacting a provider. Affected Python coverage has 112 unique passes and two reproduced baseline fixture failures (test_choice_survives_conversation_reopen, missing owned_session). Eight new source regressions pass; seven fail against committed source functions. Lint/type checks add no findings against committed HEAD. Actual provider behavior and hands-on terminal acceptance remain pending.

## Portable package

With PowerShell 7, stage an already-built executable into a new directory:

```powershell
./stage-package.ps1 -Destination 'C:/packages/litetui-rust' -Executable './target/release/litetui-rs.exe'
```

The package contains tracked runtime resources and helper tools. From the relocated directory, `./launch.ps1 -VerifyOnly` checks the complete manifest without starting Python. Launch with `./launch.ps1 -Python '<environment>/Scripts/python.exe' -Backend claude -Model '<selected-model>'`; optional Tools, ToolProfile, Workspace, DataRoot, Resume and Timeout preserve the corresponding live options. Data stays outside the package. Helper outputs follow that data root, and imports do not write bytecode into the verified source tree.

Python/dependencies remain external. Hashes verify the supplied files but do not establish that the executable was built from the recorded source. Clean installation, release provenance and terminal-host acceptance remain pending. The [current plan](../../docs/Plans/rust-tui-port/master.md) records verification and the remaining parity work.

## Backend launch options

Live startup accepts the source's nine backends: custom, lmstudio, llamacpp, ninfer, strata, codex, claude, cline and free. Select the exact model explicitly when changing the checkout launcher's default Claude backend. The portable launcher always requires both backend and model.

For example, connect to an existing LM Studio server:

```powershell
./open-chat.ps1 -Backend lmstudio -Model '<exact-model-id>' -BaseUrl 'http://127.0.0.1:1234' -ServerMode connect
```

Local backends default to connect-only. Explicit local auto mode is refused because it can discover or start a server. `-StartServer` (or `-ServerMode start`) and `-LoadModel` forward explicit requests to Python's existing lifecycle and model-loading controls. For llama.cpp, ContextLength also requires explicit server start because applying it rebuilds the router preset; connect-only loading can use an existing preset. NInfer hardware checks, Strata configuration, endpoint/file validation, owned service rules and readiness remain source-owned. An unavailable selected model blocks startup while preserving saved identity.

Both PowerShell launchers forward BaseUrl, ServerMode/StartServer, LoadModel, ContextLength, MaxTokens, ServerExecutable, ModelPath, ServerCommand, ApiKeyEnv, ServerTimeout, CodexEngine and ThinkingLevel. ServerCommand is a JSON argument array, passed without shell evaluation. ApiKeyEnv names an environment variable; it is not a credential value. The direct Rust CLI uses the corresponding hyphenated flags; start-server and load-model are boolean flags.

Fresh generic/local launches use the source's default thinking choice. Fresh Claude/Codex launches retain low unless explicitly overridden. An omitted thinking level on resume preserves the saved choice. CodexEngine accepts native or http; omission preserves the source's flag/environment/saved precedence. An explicitly unsupported level fails instead of selecting another one.

These paths have controlled source/child-process coverage. Installed engines, provider accounts, real inference and terminal-host behavior still require acceptance. F9 backend switching, F10 budget editing and explicit F12 engine controls are described below. Broader settings and engine configuration remain open.

## Open interactive live chat

Run in a terminal:

```powershell
& 'C:/Projects/.worktrees/OpenBolt-rust-tui/rust/litetui-terminal/open-chat.ps1'
```

To enable source-authorized tools in a chosen workspace:

~~~powershell
& 'C:/Projects/.worktrees/OpenBolt-rust-tui/rust/litetui-terminal/open-chat.ps1' -Tools on -ToolProfile strict -Workspace 'C:/Projects/your-project'
~~~

Tools default off. Without -ToolProfile, a native picker asks before Python starts; Escape or Ctrl+Q cancels without creating runtime state. Arrow keys or a click select a row, and a fresh Enter confirms. Redirected launches must supply --tool-profile. Strict asks before sensitive actions; interactive and autonomous are also explicit source-profile choices. The runtime must confirm the requested mode/profile, workspace and approval-host route before any prompt is sent. Startup also negotiates management protocol 1 and reads the actual source state before enabling the composer. Older sources without this contract fail with an update diagnostic. External MCP is still off. This launcher creates fresh data by default; use -DataRoot and -Resume for the saved restart path above.

Or double-click **`open-chat.cmd`** for a new dark Windows Terminal window. Both build current source and start Claude `claude-sonnet-5-5` in a new retained `C:/Projects/.scratch/T0565/claude-chat-<guid>` root. No local model service is queried or started. Pass `-Python <exe>` to the PowerShell launcher to override the interpreter.

- Type a prompt; **Enter** sends it. Replies stream into the existing themed assistant-card geometry. Subsequent prompts retain the real Python/Claude conversation.
- **Esc** while a turn is active opens stop confirmation; **Ctrl+Esc** reaches it during a question or approval. Keep going is selected by default. A second confirmed stop forces interruption. The session waits for source settlement before accepting another prompt, retains partial output, and never replays the stopped prompt. Stopping does not undo earlier tool effects.
- **Ctrl+Q** requests shutdown and waits for the contained runtime process tree. Draw/input errors also run an unconditional cleanup epilogue; UI and cleanup failures are both preserved.
- While a turn is busy, another Enter preserves the draft rather than queueing/retrying it. Live submission preserves composer whitespace. History, editing, selection, clipboard and scrolling reuse the existing implementations. **F4** now uses source-owned theme selection and persistence.
- The footer now projects validated runtime facts: reported model, conversation prefix, optional thinking level, context counts and optional settled token rate. A ready context ceiling is labelled `max`, never used as a percentage denominator. Only a confirmed usage maximum enables percent; a later missing maximum clears it. Unknown seat/authority/plan/cache/task facts stay absent. The thinking action opens the verified selector; other footer actions remain inert, and closed sessions clear controls. This is the source-default ordering, not user-preference transport or complete footer parity.
- Reported reasoning now uses the existing dashed thinking-card geometry. Claude's additive `reconcile: append|replace` wire field keeps authoritative snapshots from duplicating a prefix; older ambiguous Claude reasoning fails visibly rather than guessing. Generic unannotated deltas remain append-only. Reasoning never substitutes for an answer or completion receipt. It shares answer-text limits; no timing/token counts are fabricated. Thinking fold controls, preferences and timing remain open.
- Native tool **display records** now retain per-turn IDs, input snapshots, nullable reported failure and first-seen transcript position between assistant cards. Explicit `card_index` ownership keeps late answer/reasoning corrections on their original card. Orphan results say the request is unavailable; pending tools become incomplete at turn end/closure. These observations never dispatch tools or grant approval. Tool folding/timing and live-provider tool verification remain open.
- Identified RPC approvals now have a separate live modal: **Deny** is initially selected; Tab/Left/Right selects **Allow once**, Enter sends one decision, Escape denies, and Ctrl+Q quits. Up/Down/PgUp/PgDn scroll the full escaped request. Typing, paste, clipboard chords and key repeats cannot grant permission. The prompt remains pending until a source closure event, not merely a command ACK. Concurrent requests retain their own IDs; stale intents never target the next prompt. Always-allow and approval mouse controls remain open.
- A source approval stop now leaves the interactive session ready for a new explicit prompt once all approval closures and decision ACKs settle. The stopped row retains its reason and partial reply across follow-ups and shutdown; it is never labelled completed. Late ACKs preserve that stop outcome. Refusal may arrive before a local request or without answer text. Reasons are capped at 16 KiB and share transcript/display budgets. One-shot mode still reports refusal as failure and cleans up.
- Structured source questions now open a separate form: Tab/Up/Down changes focus, Left/Right on the question tabs changes questions, Enter/Space selects, and notes support typing/paste. Single/multiple choices, free-text, masked secret notes, Clear, Submit, Chat about this and Cancel are supported. Submit requires all questions answered; Chat permits partial answers; Escape cancels without sending notes. Mouse press/release must target the same unchanged control. ACKs close answered forms; source cancellations and late replies remain correlated. Input never falls through to the saved chat draft. Ctrl+Q still quits cleanly. Requests are bounded to 8 questions/10 options each/16 KiB text, notes to 1,024 characters each and 16 KiB combined, and the session to 100 requests with 16 pending. No automatic answers or persistent question history are added.
- Tools can now be enabled explicitly with --tools on --workspace <absolute-directory> and --tool-profile strict|interactive|autonomous (explicit selection required); the footer reports actual source policy. External MCP remains off. F8 opens owned saved conversations and F11 routes supported commands and prepared skills. Remaining footer actions/preferences, other management screens and later snapshot refresh remain open. Unsupported runtime events fail visibly; no implicit approval or mock tool action occurs.
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

Direct syntax: `litetui-rs --interactive --python <exe> --source <repo> --data-root <absolute directory> --backend claude --model claude-sonnet-5-5 [--timeout 180]`. It requires a real terminal; prompts come from the composer, not `--prompt`.

## Run the real one-prompt path

From PowerShell, one command (builds current source; requires the existing Python environment and Claude authentication):

```powershell
& 'C:/Projects/.worktrees/OpenBolt-rust-tui/rust/litetui-terminal/open-live.ps1'
```

Optionally pass `-Prompt 'Your short prompt'` or `-Python 'path/to/python.exe'`. This uses Claude `claude-sonnet-5-5`; no automatic fallback or retry. Each invocation creates a **new** `C:/Projects/.scratch/T0565/claude-live-<guid>` data root, retained for diagnosis. Its settings contain explicit isolated overrides layered on shipped defaults, never a copy of live settings. This launcher keeps tools and external MCP disabled by default. This is ordinary owned CLI startup, not a constructor/ownership bypass.

The direct binary interface is `--live --python <exe> --source <repo> --data-root <absolute directory> --backend <claude|codex> --model <slug> --prompt <text> --tool-profile <strict|interactive|autonomous> [--timeout <1..600 seconds>]`. It also works with redirected stdout when the profile is supplied explicitly. Claude was verified; Codex is not live-verified by this milestone.

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

The terminal defaults to the source-derived **Ash** palette: borderless amber tool rows on black; rounded gray assistant frames; nested dashed thinking blocks. Ryan accepted this revised visual direction on 2026-10-15 after rejecting the initial generic card redesign. Preserve the layout while wiring runtime events. F4 now offers **36 public dark themes**, generated from LiteTUI and Textual definitions without reading live settings. Search by name, use arrows, then Enter to apply; Escape cancels. F5 replays the fixture without resetting the theme. Semantic tokens drive the same layout across palettes, including resolved thinking/tool/footer overrides. Legacy fixed argument/result grays remain intentional. Selection is in-memory only: custom-theme editing, runtime catalog loading and saved selection are available in live mode below; this preview remains offline. Synthetic override tests are not custom-theme integration. The terminal uses an explicit dark palette. A cleanup guard restores raw mode, mouse capture, bracketed paste, alternate screen and cursor on normal exit or recoverable errors. Panic handling attempts restoration. Force termination cannot guarantee cleanup.

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


The controlled-process gates require Python 3.11 and an environment with LiteTUI dependencies. Set LITETUI_TEST_PYTHON to the Python executable and LITETUI_AUTHORITY_PYTHON to the dependency environment. Run each controlled target separately with cargo test --locked --test <target> -- --ignored --test-threads=1: live_process, backend_process, backend_switch_process, settings_edit_process, command_process, engine_process, conversation_process, theme_process, history_process, history_inspection_process, conversation_rewrite_process and launchers. They cover distinct runtime paths; live_process alone is not the full gate. The two launchers tests are controlled PowerShell forwarding/tripwire checks. Only the separate live_claude target remains a paid opt-in provider smoke and is excluded. History package checks additionally accept LITETUI_TEST_RUNTIME_SOURCE, binding every child report to the selected runtime source.

### Runtime backend and settings controls

F9 reads the source backend catalog. Select a backend, enter its exact model ID,
and confirm the complete displayed choice. Switching connects to an available,
already-loaded model; it does not start a local server or load weights. Finish or
resolve pending work first. An invocation-owned local server must be stopped
explicitly before switching. Current thinking must be supported by the target.

The source owns publication and cleanup. Rust verifies the resulting connection,
policy, history, catalog and held input before updating the view. The draft and
history remain available. A stale selection requires refresh and fresh confirmation;
an ambiguous transition closes the runtime and requires an explicit reopen.

F10 shows requested and effective settings, source locks and canonical immediate, next-turn, reload, reconnect or restart
timing. Use Up/Down to select a field and PageUp/PageDown to inspect long
values, R to refresh and Escape to close. Canonical credential fields are masked.
For max_tokens_chat or max_tokens_tools, E opens an output-budget draft. Enter
validates it; inspect the complete old/new confirmation and press a fresh Enter to
save. The source reports conversation scope and next-turn application. Launch,
per-model, environment or provider-owned controls disable incompatible edits.

Saved and effective values are checked independently and refreshed in the inspector.
A stale review requires refresh and new confirmation; no automatic retry occurs.
A partial runtime failure shows the saved value and still-effective value separately.
An uncertain write/application requires reopening the runtime. The chat draft is
preserved. Broader settings editing remains pending; explicit local engine controls are described below.


### Source commands and prepared skills

In idle chat, F11 opens the source command palette. Type to search; Up/Down,
PageUp/PageDown or paired clicks select rows. Tab switches commands and skills;
F5 refreshes, Left/Right inspect details, and Escape closes. Supported commands
open the existing native controls. Unsupported plugin actions show a reason.
Raw slash commands use the same source resolver, including aliases; unknown or
unsupported commands never become model prompts.

Select a skill to edit its arguments in a separate lossless draft. Paste preserves
multiline text; Alt+Enter adds a newline. Enter prepares the exact source file,
then PageDown inspects the complete prepared text and provenance. A fresh Enter
on the final preview page verifies the source again and submits normally. Tab
returns to arguments; F5 refreshes; Escape cancels, including while verification
is pending. Preparation itself never sends a prompt. The original chat draft is
preserved. Skills sharing command names remain available in the skills view or
through /skills name; direct slash commands keep their source precedence.

Invalid, changed or oversized files/payloads are refused without truncation or
automatic retry. Existing native transcript/display limits can also refuse a large
prepared skill while leaving the session usable. Source whitespace, frontmatter,
directory binding and argument suffix are preserved through normal RPC admission.
These paths have controlled source/process and TestBackend coverage; remaining
plugin screens and real-provider/terminal-host acceptance are still open.


### Explicit NInfer and Strata engine management

Use -EngineManagement with -Backend ninfer or strata, an exact -Model, and connect
mode. Configure the executable/artifact or Strata config through the existing launch
options/settings. The direct Rust flag is --engine-management and requires
--interactive. This option cannot be combined with initial prompt, StartServer or
LoadModel. An unavailable selected engine opens a management view with prompts
disabled; unrelated startup failures remain errors.

The management-only inspector opens at launch; F12 reopens it. Connected chat also
offers /engine. Up/Down or Tab selects Start/Stop; Enter requests review,
PageUp/PageDown inspects every page, and a fresh Enter on the final visible page
confirms once. R or F5 refreshes. Escape cancels a review; pointer selection alone
never executes an action.
The source rechecks the exact choice and applies its existing resource/ownership
gates. A changed review needs refresh; there is no automatic retry. A second-instance
confirmation requirement currently refuses with a reason instead of approving it.

After start, prompt entry opens only after independent engine, model, session,
history and policy checks. Stop targets only the process owned by this invocation.
An attached service cannot be stopped here. Unconfirmed cleanup retains its handle
and permits a newly reviewed stop retry. While this engine capability is active,
changing session/backend or legacy engine configuration requires an explicit reopen.

Ctrl+Q and runtime EOF cancel and join any pending engine start before owned cleanup.
A hard timeout still uses the existing contained-process shutdown fallback and
reports unproven cleanup. File/header metadata is not a full model hash; neither
successful shutdown nor these controlled tests proves VRAM release or installed
engine acceptance. Broader engine configuration and resource-confirmation UI remain.

In management-only mode F7 still permits source-offered cancellation or resolution
of held input, with durable readback before a new engine review. Continue remains
disabled until prompt-ready. Unsupported cross-provider uncertain entries stay held; this slice offers no
adapter for them. Recovery must use an authorized original-provider path. Nothing is
automatically cancelled or replayed.

### Reviewed New and Rename

In live chat, open F8 and press N for a new session or E to rename the current one.
Pending conversations are supported. Rename has its own lossless title draft; the
review shows the exact normalized saved name. Read each review page, then press a
fresh Enter on the final page to apply once. F5 refreshes and Escape closes the
review. The main chat draft is preserved. /new, /clear and /reset open the same review;
/rename may prefill the same title editor without applying it.

New settles the old provider and lifecycle hooks, creates the reviewed UUID and
rebinds conversation/task persistence. Rename preserves history and provider
continuity. Both require independent source readback before chat resumes. No new
provider turn runs until a fresh explicit prompt. Active work, held input and an
engine-management capability prevent session mutations; uncertain outcomes require
reopening rather than automatic replay.

Ctrl+Q and runtime EOF join a pending session action and provider/hook cleanup
before storage release. Shutdown proof explicitly leaves saved effects unverified;
after terminal restoration, an interrupted-action notice directs you to reopen and
inspect the saved session. Missing cleanup proof is a failure and retains ownership
until the existing bounded process supervisor takes over. Controlled subprocess
coverage is separate from real-provider and both-terminal-host acceptance.


### Persistent live themes and custom colors

F4 opens the source theme catalogue. Enter reviews a selection; F2 creates a custom
theme and F3 edits the selected definition. Tab or Up/Down moves between the name
and color fields. Enter requests a source review, and a fresh Enter on its final
page saves once. F5 refreshes, F6 shows source details, and F7 lists skipped themes.
Escape returns without applying a draft; an already-dispatched save still settles.

Changes save on this device and apply immediately after verified readback. The
view distinguishes a saved palette from one that actually became active. Custom
themes can replace registered names, with explicit overwrite disclosure. Invalid
optional colors show the source fallback; unsupported transparent/terminal-relative
palettes retain their reason and can be replaced with a supported choice. Startup
uses the verified effective source palette or a labelled fallback. Offline preview
themes remain fixture data.

F11's Theme entry opens the same controls. /new, /clear and /reset open New's
confirmation, and /rename can prefill its title editor. These routes only open
controls; another explicit review/save is required before changing saved state.
