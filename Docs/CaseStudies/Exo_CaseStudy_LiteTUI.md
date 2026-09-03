# Case Study: Exo Patterns for LiteTUI

Intent

- This is a case study of **exo** (github.com/exoharness/exo, HEAD `7801005`, cloned 2026-09-03 to
  `E:\SAS\REPO_CLONES\exo`), written as a pattern reference for **LiteTUI** — specifically for
  how a long-lived local agent keeps a canonical history it cannot erase, exposes its own
  runtime to itself, and restarts itself without losing a turn.
- Excluded: exo's chat adapters (Discord, Slack, WhatsApp, Signal, IRC), the website, the
  Firecracker/cloud sandbox backends, and Braintrust tracing. LiteTUI runs one operator on one
  box; the multi-tenant sandbox fleet is not the pattern being lifted.
- Exo is Rust (101 files) + TypeScript (74 files), 330 tracked files. The substrate is Rust
  (`crates/exoharness`), the turn loop is Rust (`crates/executor`), and the agent that runs on
  top of it — prompts, tool registry, self-map — is TypeScript under `exo/`.

Scope and evidence sources

- `E:\SAS\REPO_CLONES\exo\Cargo.toml:2` — workspace members: cli, cost, executor, exoharness, firecracker-protocol, firecracker-guest, scheduler-runner.
- `E:\SAS\REPO_CLONES\exo\README.md:31` — "The only thing it can't muck with is an event log."
- `E:\SAS\REPO_CLONES\exo\docs\RSI.md:39` — the canonical state is an immutable append-only log; `:45` — the exo-harness is the only part the agent cannot modify.
- `E:\SAS\REPO_CLONES\exo\docs\SELF-CONTROL.md:22` — durable mutations go through named tools, never ad-hoc edits; `:46` — restarts drain via a marker; `:56` — the durability table (what survives rewind / restart); `:75` — rewind does not erase history; `:76` — git is the second immutable log; `:86` — agent memory is one JSON artifact.
- `E:\SAS\REPO_CLONES\exo\exoharness\docs\spec.md:7` — executor owns semantics, exoharness owns state; `:49` — time travel; `:75` — the durable conversation is not the prompt; `:79` — the six-step turn contract.
- `E:\SAS\REPO_CLONES\exo\exo\SELF.md:62` — tool architecture (two layers); `:85` — a Rust match arm alone is invisible to the model.
- `E:\SAS\REPO_CLONES\exo\crates\exoharness\src\types.rs:178` — `TurnHandle` trait; `:257` — `TurnRecord`; `:289` — `EventKind`; `:372` — `ForkThreadRequest`; `:407` — `Event`; `:425` — `EventData` (20 variants); `:536` — `Custom` variant; `:596` — `Artifact`.
- `E:\SAS\REPO_CLONES\exo\crates\exoharness\src\basic.rs:2699` — `begin_turn`; `:2783` — `get_events`; `:2819` — `watch_events`; `:2847` — one JSON file per event id; `:3191` — `append_events_internal`; `:3198` — the write lock; `:4462` — `append_events_to_conversation`; `:4492` — `created_at` derived from the uuid7 id; `:4516` — `ensure_conversation_head`; `:4525` — `ConversationHeadMismatch`.
- `E:\SAS\REPO_CLONES\exo\crates\exoharness\src\storage.rs:17` — `BasicObjectStore` over `object_store`; `:31` — `put_json`.
- `E:\SAS\REPO_CLONES\exo\crates\exoharness\src\protocol.rs:79` — the `Request` enum (the whole wire API); `:240` — `ConversationAddEvents`; `:245` — `ConversationFork`; `:292` — `TurnAddEvents`; `:306` — `TurnFinish`.
- `E:\SAS\REPO_CLONES\exo\crates\exoharness\src\http\server.rs:59` — the HTTP surface is `/health` plus one request route.
- `E:\SAS\REPO_CLONES\exo\crates\executor\src\executor_types.rs:32` — `max_tool_round_trips`; `:92` — `SandboxScope`; `:130` — `effective_sandbox_scope`; `:154` — `ModelClient` trait; `:166` — `ToolRuntime` trait; `:226` — `ToolDefinition`.
- `E:\SAS\REPO_CLONES\exo\crates\executor\src\basic.rs:74` — `materialize_prompt_history`; `:128` — `run_turn_loop`; `:140` — the round-trip cap; `:185` — `complete_model_round`; `:291` — `execute_tool_round`; `:486` — `interpret_model_response`; `:577` — `collect_tool_requests`.
- `E:\SAS\REPO_CLONES\exo\crates\executor\src\harness_executor.rs:210` — `send_stream`, where a user message becomes a turn.
- `E:\SAS\REPO_CLONES\exo\crates\executor\src\harness_tool.rs:121` — the tool dispatch `match` on function name; `:211` — `snapshot_sandbox`; `:225` — `rewind_sandbox`.
- `E:\SAS\REPO_CLONES\exo\crates\cli\src\tui.rs:448` — `/snapshot` REPL command (the `/rewind` arm follows in the same block); `:473` — `/teleport`; `:509` — "turn failed" is printed and the loop continues.
- `E:\SAS\REPO_CLONES\exo\crates\cli\tests\repl_model_error.rs:5` — the test's stated contract; `:129` — asserts the REPL exits clean after two failed turns.
- `E:\SAS\REPO_CLONES\exo\exo.sh:29` — the repo is mounted into the sandbox at `/workspace/exo`; `:36` — scheduler interval default 10 s; `:457` — `ensure_scheduler`; `:634` — `ensure_self_repo_mount`; `:862` — `run_repl`; `:891` — `exec` of the Rust binary.
- `E:\SAS\REPO_CLONES\exo\exo\harness.ts:23` — `prompts/me.md` is loaded as identity; `:32` — `exo/SELF.md` is the self-map.
- `E:\SAS\REPO_CLONES\exo\exo\tools\memory-tools.ts:21` — `MAX_ENTRIES = 200`; `:91` — `remember`; `:130` — oldest entries dropped past the cap; `:143` — `forget`.
- `E:\SAS\REPO_CLONES\exo\exo\tools\introspection-tools.ts:54` — `list_conversation_events`; `:56` — its default kinds and the cost-from-events use.
- `E:\SAS\REPO_CLONES\exo\exo\tools\guardian-tools.ts:44` — `rebuild_and_restart_exo`.
- `E:\SAS\REPO_CLONES\exo\exo\tools\sandbox-tools.ts:61` — `snapshot_sandbox` definition; `:70` — `rewind_sandbox` definition.
- `E:\SAS\REPO_CLONES\exo\exoharness\typescript\harness\built-in-tools.ts:120` — `inspect_tools`; `:146` — `manage_tool`.
- `E:\SAS\REPO_CLONES\exo\exoharness\typescript\harness\skill-tools.ts:218` — `install_skill`.
- `E:\SAS\REPO_CLONES\exo\exo\scripts\exo-service-guardian:136` — the scheduler restart marker path; `:140` — the adapter marker; `:291` — drain-before-kill; `:318` — the fallback when a runner never claims the marker.
- LiteTUI side, for the adaptation section: `C:\Projects\LiteTUI\src\litetui\paths.py:47-53` — the per-conversation store; `:56-58` — the generated router ini; `C:\Projects\LiteTUI\src\litetui\runtime_log.py:3` — the metadata sink is not a transcript; `:33` — allowed keys; `:83` — `sanitize_event`; `:243` — `.logs/runtime.jsonl`; `:249` — `.logs/runtime-errors.log`; `C:\Projects\LiteTUI\src\litetui\conversation.py:24` — `convo.jsonl`.

Evidence glossary

- Each evidence line names the line that proves the claim and, where the line opens a block,
  what the block does. Line numbers were confirmed by reading the file at HEAD `7801005`.
  Where the scouts' reading and the file disagreed, the file won and the claim was dropped.

---

## 1) Framing: a substrate the agent cannot edit, under an agent that edits everything else

- Exo splits the harness in two. The **exoharness** owns durable state — conversations,
  sessions, turns, events, artifacts, bindings, secrets, sandboxes. The **executor** owns
  semantics — prompt assembly, model calls, tool loop, compaction. Evidence:
  `exoharness\docs\spec.md:7`.
- The whole thesis is one sentence: the log is the one thing the agent cannot touch. Evidence:
  `README.md:31`; `docs\RSI.md:39` (append-only, "nothing can erase"); `docs\RSI.md:45` (the
  exo-harness "is the only part of Exo which cannot be modified by the agent").
- That boundary is **policy, not physics**: the footnote admits the system technically allows
  the agent to modify the harness and the default configuration forbids it. Evidence:
  `docs\RSI.md:59-62`.
- The agent sees its own source: the repo is mounted into its sandbox at `/workspace/exo` and
  a hand-written self-map tells it where its own parts are. Evidence: `exo.sh:29`, `exo.sh:634`
  (block: adds the read-write mount), `exo\harness.ts:32`, `exo\SELF.md:62`.

## 2) The event log (what "canonical history" means in code)

- An `Event` is `{id, thread_id, session_id, turn_id, created_at, data}`; `data` is a
  20-variant tagged enum (thread/session/turn lifecycle, `Messages`, `ToolRequested`,
  `ToolResult`, `Error`, `ArtifactWritten`, sandbox lifecycle, process events) plus a `Custom`
  escape hatch. Evidence: `types.rs:407`, `types.rs:425`, `types.rs:536`.
- Event kinds are string constants with alias matching (`conversation_*` → `thread_*`), so a
  renamed kind still queries. Evidence: `types.rs:289`.
- **One JSON file per event**, named by a uuid7 id, under the conversation's `events/`
  directory; the timestamp is derived from the id, so ordering and time are the same fact.
  Evidence: `basic.rs:2847`, `basic.rs:4492`, `storage.rs:31` (block: `put_json` writes the
  serialized blob through `object_store`).
- **Append is the only write.** The `Request` enum is the entire wire API; it carries
  `ConversationAddEvents` and `TurnAddEvents` and no update or delete of an event. Evidence:
  `protocol.rs:79` (block: every request the server accepts), `:240`, `:292`.
- Appends are serialized under one harness-wide write lock, and a concurrent writer is
  detected by comparing the conversation's head event id before the append. Evidence:
  `basic.rs:3191` (block: lock, load record, append, write record back), `basic.rs:3198`,
  `basic.rs:4516` (block: `ensure_conversation_head` returns `ConversationHeadMismatch` at
  `:4525` when the head moved).
- Reads are a typed cursor scan with kind filters and direction, plus a live watch that
  replays existing events before streaming new ones. Evidence: `basic.rs:2783`,
  `basic.rs:2819`.
- **Rewind never erases the log.** Rewinding restores the sandbox filesystem only; events,
  artifacts, adapter and scheduler records and secrets stay. Git is named as the second
  immutable log. Evidence: `docs\SELF-CONTROL.md:75`, `:76`, and the durability table at `:56`.
- **Fork is replay, not copy-and-edit**: a thread forks up to an inclusive event id into a new
  thread. Evidence: `types.rs:372`, `protocol.rs:245`.
- The "recursive loop" claim is passive, not active: the code that prevents an agent from
  repeating itself is (a) the log it can read back and (b) a per-turn cap on tool round-trips.
  There is no cycle detector. Evidence: `executor\basic.rs:140` (the cap), `executor_types.rs:32`
  (the setting), `introspection-tools.ts:54` (the read-back tool). "Unclear from evidence"
  whether anything else enforces it — nothing found.

## 3) The turn loop (executor semantics)

- A user message becomes a turn in `send_stream`: load config, `begin_turn`, spawn the executor
  with a streaming event channel. Evidence: `harness_executor.rs:210`, `basic.rs:2699` (block:
  creates the `TurnRecord`, appends `SessionStarted` if new, `TurnStarted`, and the input
  `Messages` before any model call).
- The loop per round: materialize history from events → build the request → call the model →
  interpret the response into events → append → collect tool requests → execute tools →
  append results → stop when no tools remain or the cap is hit. Evidence:
  `executor\basic.rs:128` (block: `run_turn_loop`), `:74`, `:185`, `:486`, `:577`, `:291`.
- **The durable conversation is not the prompt.** The spec says a conversation may hold
  millions of raw events while the executor sends a compacted slice; compaction is a custom
  event pointing at a derived view, never a rewrite of history. Evidence:
  `exoharness\docs\spec.md:75`, `spec.md:79` (block: the six-step turn contract).
- Tool dispatch on the Rust side is a single `match` on the function name; the model only sees
  tools registered in the TypeScript registry, so a Rust arm without a TypeScript definition is
  invisible. Evidence: `harness_tool.rs:121` (block: the arms), `exo\SELF.md:85`,
  `executor_types.rs:166` (`ToolRuntime`), `executor_types.rs:226` (`ToolDefinition` carries
  raw JSON Schema).
- Cost rides the log: each `messages` event carries a usage annotation with `cost_usd`, and the
  introspection tool's own description says to sum it to answer "what did this cost". Evidence:
  `introspection-tools.ts:56`.

## 4) Self-modification surfaces (the part LiteTUI does not have)

- **Named tools, not file edits.** The design rule: durable mutations go through tools with
  schemas because "a mutation path that bypasses the tools also bypasses the record". Evidence:
  `docs\SELF-CONTROL.md:22`.
- The surfaces, each a registered tool: `remember` / `forget` over one JSON artifact capped at
  200 entries with the oldest dropped; `install_skill`; `manage_tool` / `inspect_tools`;
  `snapshot_sandbox` / `rewind_sandbox`; `rebuild_and_restart_exo`. Evidence:
  `memory-tools.ts:91`, `:143`, `:21`, `:130`; `skill-tools.ts:218`; `built-in-tools.ts:146`,
  `:120`; `sandbox-tools.ts:61`, `:70`; `guardian-tools.ts:44`.
- Memory is deliberately **not** retrieval: the whole store is injected every turn as a
  developer message with ids, because a small set of facts is easier to audit than an index.
  Evidence: `docs\SELF-CONTROL.md:86` (block: storage, write path, read path).
- Identity is a prompt file the agent can read and edit like any other artifact. Evidence:
  `exo\harness.ts:23` (`prompts/me.md`).
- **Restart is a drain, not a kill.** The guardian writes a marker file; the runner claims it by
  deleting it, finishes in-flight work, exits on its own; only a runner that never claims the
  marker gets its process tree stopped. Evidence: `exo-service-guardian:136`, `:140`, `:291`
  (block: drain-then-fallback), `:318`; `docs\SELF-CONTROL.md:46`.
- The REPL exposes the same time-travel to the human: `/snapshot`, `/rewind`, `/teleport`
  (snapshot under one sandbox provider, restore under another). Evidence: `tui.rs:448`,
  `tui.rs:473`.

## 5) Error strategy and tests

- A failed turn prints `turn failed: …` and the loop continues; there is no retry and no
  dead-letter. Evidence: `tui.rs:509`.
- That is a tested contract: an integration test pipes two lines into the REPL against a model
  that returns 500 twice and asserts a clean exit. Evidence: `repl_model_error.rs:5` (block: the
  test's own statement of the rule), `:129`.
- The scheduler is a separate process polling a SQLite store every 10 seconds by default and
  waking conversations; it is not inside the turn loop. Evidence: `exo.sh:36`, `exo.sh:457`.
- The HTTP surface is two routes: `/health` and one JSON request endpoint that carries the
  whole `Request` enum. Evidence: `http\server.rs:59`, `protocol.rs:79`.

## 6) What the reference does that the code does not prove

- "Full lineage across clones" (`docs\RSI.md:39`) — thread fork exists (`protocol.rs:245`);
  sandbox *cloning* (a second sandbox from a snapshot while the first keeps running) is listed
  as a gap in exo's own doc. Evidence: `docs\SELF-CONTROL.md` §4 (the "Gap" paragraph).
- "Cannot get stuck in recursive loops" — see §2, last bullet: the log makes repetition
  *visible*, the round-trip cap makes one turn *finite*; nothing makes a second turn refuse to
  repeat the first.

---

## 7) LiteTUI design notes (adaptation guidance)

These are recommendations based on the observed patterns above, not requirements.

- **LiteTUI already has the split, by accident of naming; make it a rule.** The per-conversation
  store — `convo.jsonl` (append-only transcript), `memory.md`, `soul.md`, `handoff.md`,
  `memories/` — is exo's conversation/artifact/identity layer, and `.logs/runtime.jsonl` with
  its allowlisted keys is exo's lifecycle/host event stream. Evidence: `paths.py:47-53`,
  `runtime_log.py:3`, `:33`, `:83`, `:243`. Pattern takeaway: write down which files are
  *history* (never rewritten, never pruned by the agent) and which are *derived* (the router
  ini at `paths.py:56-58` is already documented as derived output). Exo's durability table
  (`SELF-CONTROL.md:56`) is the shape to copy: one row per store, what survives what.
- **Give the transcript event ids, not just order.** Exo's uuid7-per-event means the id *is* the
  timestamp and a cursor can address any point (`basic.rs:2847`, `:4492`, `:2783`). LiteTUI's
  `convo.jsonl` is line-ordered; a `/rewind`-style feature or a "fork this conversation from
  here" needs an id per line first. Pattern takeaway: add an id field and a `get_events`-style
  cursor read before building anything on top of replay.
- **Head-check before append.** LiteTUI already fought one shared-file write race (the router
  record: "our write must not erase a live peer's claim", LiteTUI `7de4655`). Exo's answer is
  generic: compare the head id, refuse on mismatch (`basic.rs:4516`, `:4525`). Pattern
  takeaway: the same three lines guard `convo.jsonl` if two LiteTUI processes ever share a
  conversation directory.
- **Lifecycle events belong in the log, not only in the errors file.** Exo records restarts,
  drains, snapshots and rebuild outcomes as events the agent can query with
  `list_conversation_events` (`introspection-tools.ts:56`). LiteTUI's `runtime.jsonl` records
  failures (`test_runtime_log_producers` enumerates the eighteen names) but not "I restarted",
  "I compacted", "I loaded model X". Pattern takeaway: add the positive lifecycle kinds to the
  allowlist so `/autocompact`, `/mcp` start/stop and router loads leave a row.
- **Memory as a capped, fully-injected list is the right size for LiteTUI.** Exo's `remember`
  is 200 entries, oldest dropped, whole store in the prompt each turn (`memory-tools.ts:21`,
  `:130`; `SELF-CONTROL.md:86`). LiteTUI's `memory.md` index already does the "index is
  injected, notes load on demand" half (`paths.py:50`). Pattern takeaway: give it the cap and
  the `forget(id)` verb; skip embeddings, as exo did, until the cap is hit in practice.
- **Drain markers for restarts.** LiteTUI kills process trees with Job Objects (`jobkill.py`)
  and has the reaper (T221). Exo's guardian asks first — marker file, runner claims it,
  finishes the turn, exits — and kills only when the marker is never claimed
  (`exo-service-guardian:291`, `:318`). Pattern takeaway: a `.restart` marker the app claims at
  the next idle prompt is cheaper than a kill and preserves the in-flight turn; the kill stays
  as the fallback it already is.
- **The self-map.** `exo/SELF.md` is a hand-written "where my parts are" the agent reads before
  editing itself (`harness.ts:32`, `SELF.md:62`), and the rule at `SELF.md:85` (a backend arm
  without a frontend definition is invisible) is exactly the shape of LiteTUI's own
  visible-and-inert findings this week (T222). Pattern takeaway: LiteTUI's `handoff.md` is per
  conversation; a repo-level `SELF.md` for the *agent's* own parts — where the tool registry is,
  where the prompt files are, what a change to each needs — is the missing document.
- **Cap the round-trips and make cost readable from the log.** The one hard loop guard in exo
  is `max_tool_round_trips` (`executor\basic.rs:140`), and cost is summed from `messages`
  events (`introspection-tools.ts:56`). The FrontierHarness data read the same day showed six
  Claude Code runs at 176–381 turns costing $26–$78 each against a median task of $0.37.
  Pattern takeaway: a per-turn round-trip cap and a `usage` field on each transcript row are
  the two cheapest guards LiteTUI can add; the Shift+Tab brake is the human half of the same
  control.
- **Do not lift:** the sandbox fleet, the TypeScript-over-Rust tool double registration, and the
  policy-only harness boundary. LiteTUI is Python on one machine; its boundary can be a real
  one (a read-only history directory the agent's tools cannot open for write) rather than a
  default setting.
