# Python-produced wire output fixtures

These nine **output-only imports** come from the existing owned capture run:

`artifacts/rust-tui-port/wire-captures/run-d2912ff6c98d47f182ea4bbbc130a073/`

This is not a copy of the complete 29-file evidence bundle. The original input descriptions, per-case manifests, completed index and pytest log remain in that run directory. Inputs are heterogeneous synthetic commands/events/helper descriptions, not a uniform executable replay format. No input is executed here. The original completed index reports nine exported cases and the historical pytest log reports `18 passed in 1.11s`; nine exports do not represent every one of those eighteen tests.

## Byte fidelity

Each local `<case>.jsonl` is a text-channel import of the original `<case>.output.jsonl`. Native file tools and Windows checkouts can represent line endings as CRLF. The Rust test reconstructs LF framing ONLY, then verifies SHA-256 against the original output digest copied from the original `completed.json` before decoding any records. **All nine reconstructed streams must match their original byte digests.** JSON spacing, escapes, values and IDs are not rewritten or reserialized. Any other transcription or payload change fails the digest assertion. This explicitly proves the replayed bytes, not byte identity of the working-tree text files or integrity of omitted bundle members. Hash pins are in `tests/wire_capture_replay.rs`.

| Case | Output records |
| --- | ---: |
| negotiated-mirrors | 6 |
| reader-framing | 5 |
| unsupported-protocol | 1 |
| cached-event-mirrors | 4 |
| approval-denial | 4 |
| operation-context | 4 |
| refusal-before-ack-raw | 2 |
| refusal-before-ack-gui | 3 |
| terminal-null-rates | 2 |
| Total | 31 |

The inspected negotiated-mirrors manifest records Python 3.11.9, win32, pytest 9.1.1, pytest-asyncio 1.4.0 and Textual 8.1.0. Its scope is selected disk sources and runtime identity, not a complete dependency lock. Original manifest digest recorded by the completed index: `fde5fa59501b10eb8ca134f5287db89a2a4ae5b71d6dfbf1e2ee38ba9ab1c154`. Its production wire-module source hashes are:

- `src/litetui/rpc.py`: `65602270434c73ea0fee4d1139c79aafb46950216a325dd3024417885ffa5d55`
- `src/litetui/gui_rpc.py`: `dd62375cff0f794596fce64e2738743cb9c106e521949e4c5fe45b5719c334c8`
- `src/litetui/app.py`: `9ad4d8d5f16cf33124e229d3f50f1450cd392740263ec92b2fc8d93600c0aa48`

## What the replay proves

The real Rust JSONL decoder receives these hash-verified bytes whole, at every two-part byte boundary, and in single-byte chunks. Tests retain all 31 records, equal repeated events, escaped text, explicit nulls, response identity, operation identity, and refusal-before-ACK ordering. The captured snow character is ASCII `\u96ea` on the wire, not a literal multibyte transport test.

The public WireClient gets literal captured IDs. Its generated request IDs do **not** match `fixture-hello`, so the captured successful hello remains unmatched and cannot establish compatibility or drain a generated pending hello. Cold literal negotiated-stream replay is deliberately fail-closed: legacy event, unmatched response, legacy event, activation-uncertain fault, two quarantined events. The event policy is a single explicitly reviewed fixture event, not an allowlist inferred from observed traffic.

No positive correlated negotiation, matched approval command, production delivery inventory, state reducer, approval authority, turn execution, cache implementation, runtime startup, fd/pipe behavior, provider/tool execution or real-child isolation is established. The source Python production wire functions ran against synthetic state; no Rust runtime or preview is launched by these tests. A successful `turn: accepted` response after refusal is NOT interpreted as execution starting.
