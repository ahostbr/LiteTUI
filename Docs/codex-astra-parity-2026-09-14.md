# Codex Astra backend parity — 2026-09-14

The source backends use the official Codex app-server for agent execution:
LiteSuite already did; LiteTUI now does, and LiteGUI consumes that LiteTUI backend.
The six supported UI efforts are `low`, `medium`, `high`, `xhigh` (Extra high),
`max`, and `ultra`. Medium is the Codex default. Max and Ultra carry usage notices.

## Why the transport changed

Live direct-OAuth requests accepted low through max but rejected literal `ultra`
with HTTP 400 (`reasoning.effort`, `invalid_value`). CLI 0.154.0 model metadata
advertises Ultra as an orchestration choice and reports Astra's
`multi_agent_reasoning_effort` as `xhigh`. Passing the label directly to the
Responses endpoint did not implement that mode.

Ryan explicitly selected migration to official app-server. The bridge now sends
`turn/start.effort` unchanged, including Ultra, and Codex owns its interpretation,
native delegation, credentials, routing, caching, reasoning history, and compaction.

The executable resolver follows PATH before looking for an executable suffix.
The previous `.exe`-first attempt bypassed the npm CLI and selected an older
desktop-bundled CLI that rejected Ultra at the app-server protocol boundary.
All successful live results below used the PATH-selected 0.154.0 installation.

## Implemented boundaries

- `/think`, model configuration, and settings expose the Codex model's advertised
  levels. Existing per-model overrides are updated when changing the current effort.
- LiteGUI continues consuming runtime capabilities; Extra high is labelled clearly,
  and Max/Ultra show higher usage notices.
- LiteSuite defaults Codex to medium and respects `CODEX_HOME` for cached model discovery.
- New turns append only input after the persisted Codex thread reference. Resume
  uses `thread/resume`; accepted user messages carry a reference before generation
  finishes, including when generation is interrupted.
- Changed host instructions use app-server application context rather than
  rebuilding prior conversation input. Initial dynamic tool definitions are
  registered once; deferred host tools use Codex's `deferLoading` registration.
- Host tool calls pass through LiteTUI's existing `_execute_tool` authorization.
  Native command/file approval requests pass through the host approval UI. Unknown
  server requests receive an unsupported-method error rather than approval.
- Closing an active stream sends `turn/interrupt`. Backend switching and application
  shutdown close the owned process. The Windows fallback targets only its own PID tree.
- Codex owns automatic compaction. Manual `/compact` calls `thread/compact/start`
  and leaves the displayed LiteTUI transcript intact.
- Title/evaluator calls use isolated Codex sessions without the main host tool context.
- Cached-input usage is preserved. Cache-write usage absent from app-server remains
  unknown (`null`), never a fabricated zero.

## Live results

Synthetic requests, no project data in the supplied prompt. Tiny `OK` replies;
these establish mode acceptance and cache reporting, not reasoning-quality scores.

| Effort | Completed | Input tokens | Cached input tokens | Seconds |
|---|---|---:|---:|---:|
| low | yes | 28,674 | 13,056 | 4.88 |
| medium | yes | 35,205 | 13,056 | 3.31 |
| high | yes | 35,220 | 13,056 | 5.19 |
| xhigh | yes | 35,235 | 13,056 | 3.11 |
| max | yes | 35,250 | 13,056 | 2.20 |
| ultra | yes | 35,368 | 35,072 | 5.53 |

The harmless dynamic echo tool executed once through the host bridge. Its final
request reported 28,544 cached of 28,777 input tokens. A follow-up live check
completed native compaction and verified the local transcript remained identical.
The probe exited successfully after closing its app-server process.

The old raw transport's repeated 2,817-token prompt reported zero cache reads;
a larger raw request reported a hit followed by a miss. This was not sufficient
evidence for optimal caching, and was not used to claim it.

## Validation and delivery limits

- LiteTUI: 136 focused regression tests passed, covering reasoning selection, transport, cache usage,
  actual Textual stream integration, persistence, interruption, native compaction,
  instruction updates, settings, and existing local-backend controls.
- New adapter/transport/probe files pass scoped Ruff checks.
- LiteGUI: all 104 tests passed; TypeScript typecheck passed.
- LiteSuite: 160 focused shared-model, model-picker, settings, app-server and adapter
  tests passed; one pre-existing app-server test skipped. Full typecheck passed;
  lint reported zero errors. Changed-file formatting check passed.
- LiteSuite's full `bun run test` failed in `scripts/preload-declaration.test.ts`
  because the checkout has undeclared preload members (including draw/GPT Live).
  This task did not edit that area. Full-suite success is not claimed.
- Existing unrelated dirty work was preserved. Changes are not committed, merged,
  or released; no running user desktop was restarted.
- LiteGUI's bundled runtime manifest still pins `f51f0be360a28ddd87937c87eb4df8c47306137d`
  (LiteTUI 0.22.2). A normal reviewed runtime/package rebuild is required to deliver
  these source changes in the packaged application. The old manifest was not
  rewritten to claim provenance for uncommitted source.

No client can guarantee a 100% cache-hit rate. The implementation delegates cache
behavior to the same official engine as the CLI, and the live results demonstrate
actual reuse. Cache writes, eviction, compaction and model/effort changes remain
subject to the service and CLI's behavior.

## References

- [Official app-server integration](https://developers.openai.com/codex/app-server)
- [Official prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
- [Reasoning updates and compatibility limits](https://developers.openai.com/api/docs/guides/reasoning#change-reasoning-mid-conversation)
- [Codex 0.154.0 request implementation](https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/client.rs)
- Version-specific schema generated locally with `codex app-server generate-json-schema --experimental`.
- Reproducible probes: `scripts/codex_app_server_probe.py` and
  `scripts/astra_cache_parity_probe.py`, each requiring explicit `--live`.
