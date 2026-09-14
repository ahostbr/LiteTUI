# Codex host integration audit — September 14, 2026

Scope: current LiteTUI app-server bridge, compared with the shared chat/tool lifecycle;
read-only inspection of LiteGUI's RPC consumer. This is a source audit plus focused
regression tests, not a claim that every installed desktop flow has been tested.

## Fixed in this audit

- Host dynamic-tool responses now use the same escape stripping and named-secret
  redaction as the local agent loop, before RPC display and before returning to Codex.
  Terminal mode restoration is skipped for headless RPC to avoid writing control bytes
  to its output pipe. This does not filter Codex's internally executed native tools
  before the model sees their results.
- Images staged by host tools now return as protocol `inputImage` content with
  `imageUrl`. Previously the native loop never reached the local between-round image
  drain, so it received only the textual confirmation. Staging is drained once;
  tool cards show an image marker rather than dumping the base64 payload.
- A quiet native turn now observes the stop flag while waiting for events and sends
  `turn/interrupt`, instead of requiring another text token. Host tools arriving after
  a stop are refused. An already-running host tool still follows its existing host
  process/dialog cancellation behavior.
- Shared stream cleanup now closes app-server streams as well as OAuth response
  streams, including exception/cancellation exits.
- Distinct assistant message IDs now produce paragraph separators, avoiding joined
  text such as `Sentinel.I'm` in the screenshot.
- Native tool-card creation now ends the shared thinking timer.

## Still incomplete or deliberately different

| Area | Current behavior and limitation |
| --- | --- |
| Native tool policy and hooks | Host dynamic tools pass through `_execute_tool`, including tool enablement, policy/approval, and before/after hooks. Codex native shell/file/MCP/delegation tools do not. Native approval requests use the host approval UI, but operations Codex permits without approval never visit that UI or host hook gate. |
| Tools off and per-tool disables | These enforce host dynamic-tool execution. The bridge uses a read-only Codex sandbox for tools-off/restricted profiles; read-only is not a prohibition on every native tool. Do not describe the switch as disabling all Codex tools. |
| Host task manager and per-tool cancel | Host subprocess tools retain background promotion, `/tasks`, and process cancellation. Codex native subprocesses are not registered in `ttyguard.CANCELLABLE` or the host task registry. Use Stop Turn for native execution; a separate native-tool cancellation equivalent is not implemented. |
| Queued input during a turn | The local loop invokes `hook_host.queued_prompt` between tool rounds. The app-server path does not bridge this to `turn/steer`; normal end-of-turn delivery remains, but mid-turn steering parity is absent. |
| Transcript and replay | Tool cards are currently live view state. Native items are not appended as local tool messages, and resumed Codex thread items are not replayed into these cards. Codex owns its history; local history/export/reopened cards do not contain an equivalent full trace. |
| Live progress | Final tool details are displayed. Command-output deltas, MCP progress notifications, native plan updates and automatic compaction notifications are not fully mapped to shared widgets. |
| Structured user answers | `requestUserInput` opens the shared question UI, but returns its human-readable answer report as one answer string. Structured selected labels/free text and cancellation need a dedicated bridge; the current report preserves cancellation wording but is not native structured parity. |
| Tool inventory changes | Dynamic tool schemas are registered on thread creation. Enabling a previously unregistered host tool later does not refresh that native thread's inventory. Execution-time disables still apply to host tools. |
| Local loop settings | Local maximum tool iterations, result-context rewriting, token caps and sampling knobs are not all consumed by app-server. The CLI owns native iteration, reasoning, compaction and request construction. The current settings UI does not adequately distinguish this. |
| Usage accounting | `thread/tokenUsage/updated.last` is forwarded to shared usage reporting. Multi-request Codex turns need separate auditing of accumulated turn usage versus latest-request context; current timing/TPS and token totals should not be claimed as exact aggregate native-turn accounting. |
| LiteGUI RPC matching | The renderer currently matches results by latest pending tool name, not protocol call ID. Concurrent native calls with the same name may be paired incorrectly there; the LiteTUI cards themselves match by ID. Packaged GUI runtime delivery is also separate from source changes. |

## Confirmed shared behavior

The normal app entry/exit still wraps the app-server stream: user prompt admission,
assistant display, reasoning display, normal conversation persistence, completion
hooks and plugin turn finalizers remain in `app.py`. Host dynamic tools retain their
shared execution authorization door. Codex thread references preserve native history
without reconstructing it into each provider request. Native caching and reasoning
remain owned by the official engine.

## Evidence

- `src/litetui/codex_app_server.py`: server request bridge, thread registration,
  native event loop, usage forwarding, interruption and response cleanup.
- `src/litetui/app.py`: `_execute_tool`, `_stream`, `_elapsed_repaint`,
  `action_cancel_tool`, staged-image/queued-input drains and `_compact`.
- `src/litetui/codex_tool_ui.py`: shared cards and item-ID matching.
- `src/litetui/ask_user_question.py`: `_serialize` and question result contract.
- `C:/Projects/LiteGUI/src/renderer/state.ts`: RPC tool-call/result matching.
- Installed 0.154.0 generated JSON schema confirms `inputImage.imageUrl`.
- 41 focused tests passed: app-server transport, Codex tool cards, OAuth transport/UI,
  and shared collapsible cards. Added regressions cover cleanup/image delivery,
  stopping with no text events, and assistant-message boundaries.

Remaining gaps above are not represented as fixed. Native behavior and host behavior
need explicit integration contracts rather than routing native commands through a
second agent loop and losing the user's requested official-engine behavior.
