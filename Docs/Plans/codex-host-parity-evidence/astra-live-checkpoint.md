# Native Astra six-mode and deferred inventory probes

Task REL-20260914-CODEX-PARITY. Candidate feat/codex-host-parity at 49fded3,
plus the probe-only live-registry fixture correction in this checkpoint.
These were intentional authorized synthetic provider calls, not unit tests.

Runtime: `codex-cli 0.154.0`. Python PATH resolves
C:/Users/Ryan/AppData/Roaming/npm/codex.CMD. The adapter's shim resolution has one
native candidate:
C:/Users/Ryan/AppData/Roaming/npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe

Executable SHA-256: be96b992178b1e467c225800da0d65f2c86d5eba1ef0b14632f65db381cbdfde.

## Six-mode completion

With PYTHONPATH=src:

```powershell
python scripts/codex_app_server_probe.py --live --efforts low medium high xhigh max ultra --usage-evidence Docs/Plans/codex-host-parity-evidence/astra-six-mode-usage.json
```

Stdout is retained in astra-six-mode-run.jsonl. Each mode completed, with zero host
tool calls. Six sequential minimal requests used the same native thread reference.
The source prompt requests only OK, no tools or file inspection. Artifacts contain
mode names, counters, booleans and timings, not model text or credentials.

| Mode | Seconds | Input tokens | Output tokens | Cached input tokens |
| --- | ---: | ---: | ---: | ---: |
| low | 4.92 | 28062 | 5 | 8448 |
| medium | 2.36 | 34593 | 5 | 8448 |
| high | 2.08 | 34608 | 5 | 12544 |
| xhigh | 2.19 | 34623 | 5 | 8448 |
| max | 2.45 | 34638 | 23 | 7680 |
| ultra | 1.83 | 34774 | 5 | 34432 |

Validated all six saved snapshots: context_tokens equals the latest native
totalTokens; each per-turn counter equals cumulative minus the previous cumulative
counter; all modes reported cache hits. cacheWriteInputTokens was explicitly zero
in the native payload, so the displayed zero here is not a missing-field default.
These measurements do not establish billing rates, maximum cache efficiency, or a
fixed cache ratio. Modes can alter native instructions and therefore reuse.

## Deferred dispatch with the inventory guard

```powershell
python scripts/codex_app_server_probe.py --live --efforts medium --deferred-tool --event-evidence Docs/Plans/codex-host-parity-evidence/inventory-deferred-live.json
```

Completed in 14.08 seconds; one synthetic echo call. Exactly one start/result pair,
matching complete native identity, unique lifecycle events, matching result/text
alias and measured duration. The probe fixture now provides tool_specs as well as
deferred_specs, satisfying the live registry verification rather than bypassing it.
Both probe processes completed with exit zero; their app-server instances closed in
the script's finally block. No Suite process was launched or restarted.

## Acceptance limits

This establishes native completion for all six effort strings and measured usage
through the candidate transport. A tiny OK request does not demonstrate automatic
Ultra delegation on demanding work, all-client setting persistence, or UI selection
fidelity. Deferred lookup/dispatch works; replacing the tool inventory within an
existing thread and migration of older threads remain open. Full C1-C10, GUI and
packaged/runtime acceptance remain required. No release clearance.
