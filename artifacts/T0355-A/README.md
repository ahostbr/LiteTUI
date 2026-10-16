# T0355-A — MCP schema exposure receipt

Measured 2026-10-15 local / 2026-10-16 UTC on `work/T0355-A`, base `0bab8f9`, with installed `codex-cli 0.159.0`.

## Result

**The reported first-request SOTS schema bloat was not reproduced on either measured route. No production change is warranted by these measurements.** Both host MCP and native Codex can still perform a real `sots_help` call after demand discovery. The original assumption that `--backend codex` necessarily uses native Codex was also incorrect.

This is a receipt of existing behavior, not a before/after improvement claim. Machine-readable measurements and raw-file SHA-256 receipts are in [receipt.json](receipt.json).

## First-request measurements

All figures are UTF-8 bytes, not estimated tokens. Schema/array sizes use compact JSON (`ensure_ascii=False, separators=(",", ":")`); index sizes use literal UTF-8 text. Per-server schema costs exclude unrelated host/native tools, ordinary instructions and array punctuation.

| Route | CORE schema bytes | BPGEN schema bytes | VibeUE schema bytes | Initial names/index bytes |
|---|---:|---:|---:|---:|
| Host MCP, cold cache | 0 | 0 | 0 | 133, server-name discovery guidance |
| Host MCP, warm cache | 0 | 0 | 0 | 5,200, complete names-only block |
| Native Codex, current Sol `code_mode_only` catalog | 0 | 0 | 0 | 0 SOTS-specific index bytes |
| Native Codex, explicit direct-mode control | 0 | 0 | 0 | 38, SOTS server-name lines within native search guidance |

Real server inventories were **CORE 8 tools, BPGEN 170, VibeUE 5**. The corresponding complete host schemas would occupy 6,701 / 129,382 / 2,746 bytes respectively, totaling **138,829 bytes** if offered eagerly. They were withheld.

The host warm names-only server lines occupy CORE 139 / BPGEN 4,619 / VibeUE 113 bytes; the rest of the 5,200-byte block is guidance and lazy-server status. No host SOTS process was running at either first-request measurement.

Native payload inspection included **prewarm frames, top-level `tools`, `input.additional_tools`, and code-mode `exec` declaration text**. Inspecting only the top-level `tools` field would have been misleading: current Sol uses Responses Lite and puts native tool declarations in `input.additional_tools`.

## Demand-discovery and real calls

### Host MCP

The actual `mcp` and `tool_search` plugins, `MCPManager`, `OpenAITransport`, and OpenAI SDK were exercised. The SDK request was intercepted with `httpx.MockTransport`, so no non-Codex model inference was performed.

1. Cold startup with a copy of the real SOTS `.mcp.json` produced the server-name index and zero SOTS schemas.
2. Real MCP `initialize` / `tools/list` populated the normal cache; reopening lazily produced all 183 tool names but still zero SOTS schemas.
3. `tool_search(query="select:mcp__SOTS_MCP_CORE__sots_help")` loaded exactly one schema.
4. The next serialized request included **212 CORE schema bytes**, zero BPGEN/VibeUE schema bytes, and only CORE running.
5. Dispatching `mcp__SOTS_MCP_CORE__sots_help({})` reached the real server and returned **`ok: true`**.

### Native Codex, current Sol code mode

An actual installed `AppServer` process ran with the real SOTS execution cwd and project registrations. A temporary `CODEX_HOME` isolated mutable state. Its Sol model catalog was copied from the installed cache without changing `tool_mode`, `supports_search_tool`, or Responses Lite settings. The existing `node_repl` registration was also copied, with its `CODEX_HOME` redirected to the temporary home. No shared Codex config was edited.

The Responses websocket endpoint was a loopback recorder with deterministic scripted responses and a fake API key. **Codex and MCP execution were real; the model responses were scripted, not paid inference.**

The first model request contained no SOTS declarations. The following native `functions.exec` calls then succeeded:

```js
// Discover names only, when needed. This is ordinary JS reflection.
text(Object.keys(tools).filter(
  n => n.startsWith("mcp__SOTS") || n.startsWith("mcp__VibeUE")
));

// In the next exec call, invoke the discovered harmless tool by name.
text(await tools.mcp__SOTS_MCP_CORE__sots_help({}));
```

Reflection returned **183 names** (CORE 8 / BPGEN 170 / VibeUE 5), occupying **8,096 compact JSON bytes only after that discovery call**. The next exec reached real CORE and returned `isError: false` and `structuredContent.result.ok: true`.

Thus zero initial SOTS schemas does **not** mean native code mode has lost the tools. They remain runtime methods and can be discovered/called by name. `tools.tool_search(...)` is **not** a callable helper in this mode; a separate negative probe returned a TypeError. Do not recommend that spelling for native code mode.

The direct-mode control changed only the copied catalog's `tool_mode` to `direct` and `use_responses_lite` to `false`. Native Codex then offered its built-in `tool_search` with SOTS source names, while still withholding all 183 SOTS schemas. Its search description was 648 bytes including unrelated `node_repl` guidance; the three SOTS source-name lines were 38 bytes.

## Why the earlier native-registration premise was wrong

- `src/litetui/settings.py:173` defaults `codex_native_engine` to **False**. Selecting backend `codex` alone does not establish a native app-server seat.
- `src/litetui/model_transport.py:1114` chooses `AppServerTransport` only for a backend with `app_server`; otherwise Codex uses `OAuthTransport`, with the host tool loop.
- `src/litetui/codex_mcp.py:69` does register scoped project servers into native Codex. That proves registration/process availability, not eager model exposure.
- Host `plugins/tool_search.py:59` enables dynamic schema deferral; `PluginRegistry.tool_specs()` returns the advertised partition.
- `codex_inventory.py:36` already represents deferred host tools as native namespace tools with `deferLoading=True`.
- Installed-version native source sets MCP exposure to deferred when the model supports tool search and the provider supports namespaces. Legacy feature flags are not the solution: `codex features list` identifies `tool_search` and `tool_search_always_defer_mcp_tools` as removed.

Pinned upstream source:

- [MCP exposure, rust-v0.159.0](https://github.com/openai/codex/blob/rust-v0.159.0/codex-rs/core/src/mcp_tool_exposure.rs)
- [Model/provider search gate, rust-v0.159.0](https://github.com/openai/codex/blob/rust-v0.159.0/codex-rs/core/src/tools/spec_plan.rs)

## Recent transcript routing

A read-only scan of 250 `.agents` conversation files containing the target SOTS terms examined messages in the last seven days. Counting **actual assistant `tool_calls`**, not paired outputs or text mentions, found:

| Tool | Assistant calls |
|---|---:|
| `sots_inbox` | 26 |
| `sots_rag` | 13 |
| `sots_run_devtool` | 4 |
| `sots_help` | 1 |
| `sots_view_screenshot` | 1 |

All of these assistant-call messages carry the legacy Codex metadata shape (`provider`, `model`, `items`), without `app_server_thread_id`. No recent native thread metadata/display-trace records were found in that matched file set. Together with the current transport code, this supports classifying the observed calls as **Codex OAuth + host MCP**, rather than native MCP or native exec. Historical versions' metadata invariants were not independently audited. The scan is not claimed as a complete historical fleet census. Paired outputs explain why rag/devtool text-record counts can be double their invocation counts; counts of arbitrary mentions are not invocation evidence.

Example: `.agents/Carmack-T0571-A/conversations/231568b6-5e03-422d-983c-6b2bf03f243a/convo.jsonl:27` is an assistant SOTS inbox call; line 28 is its matched output. Its metadata has no native thread ID. `model_transport.py`'s legacy response capture uses `provider/model/items`; native `codex_app_server.py` writes `app_server_thread_id`.

## Reproduction and evidence boundaries

The reversible probe scripts and raw captures remain in this isolated worktree under `temp-working-dir/t0355_*`. They are local investigation material, not production additions. Exact raw capture paths, lengths and SHA-256 hashes are retained in `receipt.json`.

```powershell
$env:PYTHONPATH = "$PWD/src"
python temp-working-dir/t0355_host.py
python temp-working-dir/t0355_capture_direct.py
Copy-Item temp-working-dir/t0355-capture.json temp-working-dir/t0355-direct-first.json
python temp-working-dir/t0355_capture.py
python temp-working-dir/t0355_capture_negative.py
python temp-working-dir/t0355_native_history.py
python temp-working-dir/t0355_receipt.py
```

`capture.py` uses the current local SOTS and Codex paths, starts only isolated native/MCP processes, records websocket request bodies (not authentication headers), and runs only name reflection and `sots_help`. `capture_negative.py` is a separate probe for the unavailable `tools.tool_search` helper; its raw result is `temp-working-dir/t0355-code-mode-negative.json`. The receipt builder asserts zero initial per-server schemas, exact reflected inventory count, and successful real host/native calls. Re-running timestamps/IDs change raw hashes.

The raw captures and probe scripts are **local-only and are not included in the proposed merge**. These two merged documents alone therefore do not support independent reproduction or raw-content verification after that local material becomes unavailable. The hashes identify captures when available; hashes do not independently validate capture contents.

Limitations:

- No mounted Textual UI, live non-Codex model, or paid Codex inference was exercised. This proves serialization and deterministic execution, not that a model will independently choose reflection.
- Native deferral depends on model/provider capabilities. Older Codex builds or fallback/custom models without search support were not proven lazy by these measurements.
- The initial native code-mode prompt has no SOTS-specific names index. Runtime reflection supplies one when needed; no claim is made that the names are present initially.
- No production files or tests changed. Therefore no unit suite, broad runner, or project lint/type gate was run; the relevant validation was the real transport/MCP probes and receipt assertions.
- Native websocket shutdown reported connection-close warnings after the completed turns. Both real calls and turn completion were observed before shutdown; this is not an inference-success claim based on shutdown status.
- No shared `~/.codex/config.toml` edit, push, merge, worktree removal, or newly introduced junction/symlink occurred.

Only this report and its JSON receipt are proposed for merge. Review/human-look remains the leader's gate. A standalone worker cannot claim a canvas display; the artifact is available at this path for inspection.
