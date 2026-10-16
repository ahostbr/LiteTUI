# LiteTUI Rust — offline visual preview

This is an **interactive mock conversation**, requested so the terminal layout and tool cards can be inspected before runtime integration. It is not the completed Rust port.

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
| Up / Down (completion closed) | Recall prompt history / restore original draft |
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
| Ctrl+Q or Ctrl+C | Exit and restore terminal modes |

Below 36 columns or 14 rows, the preview asks for a resize and suspends decision keys; a hidden approval cannot be confirmed. No full editor, mouse selection, markdown renderer, clipboard integration, backend selector, settings screen, image support or runtime transport is claimed yet.

The composer edits at Unicode scalar boundaries and replaces selections on insertion. Rendering uses whole graphemes: an internal scalar caret position is displayed at the glyph's trailing edge, and horizontal scrolling preserves complete wide glyphs. A single grapheme wider than the input viewport is displayed as � until it fits again; the underlying text and scalar selection remain intact. History preserves the original draft while browsing and restores it at the end; edits of recalled entries do not change stored history. Preview-only limits are 500 characters per draft and 100 history entries, not production defaults. Ordinary insertion converts CR/LF/tab to spaces and discards other controls. Bracketed paste instead takes only the first Python-style splitlines line and replaces the selection: an empty first line removes selected text, while an empty paste event does nothing. This event ownership was measured with a mounted source PromptInput: it stops the event before the application paste handler. Preview bounds and control filtering still apply, so unrestricted source-input parity is not claimed. The distinct Ctrl+V OS-clipboard path (normalized multiline insertion at the cursor, not selection replacement) remains unimplemented; no system clipboard is read or written.

Confirmation requires a key press; reported repeat events cannot confirm a modal or submit a draft. Repeats of a modal-closing letter are suppressed until release or a fresh press, while ordinary typing and cursor repeats still work. Hosts reporting repeated physical keys as fresh presses cannot provide this distinction. OS clipboard, runtime completion catalog/dispatch, footer navigation and runtime submission remain open.

Word movement/deletion follows Textual 8.1.0's `(?<=\W)\w` searches on the cursor prefix/suffix, including punctuation and selection behavior. Classification is frozen to **Python 3.11.9 / Unicode 14.0.0**, the inspected source environment—not Rust's broader/current `is_alphanumeric`, and not a guarantee for future Python runtimes. `scripts/export_rust_word_boundaries.py --check` detects stale tables/oracles. Regenerate with the supported source interpreter only; an interpreter/Unicode upgrade changes this compatibility target and needs a reviewed fixture update. The Python tests compare the oracle against actual mounted Textual Input actions; Rust tests replay every fixture cursor with both selection orientations. No runtime Unicode-version negotiation exists yet.

Slash completion is non-modal: ordinary editing keeps focus in the composer, Up/Down selects before history, Tab accepts the entire name plus a trailing space, and Escape stays dismissed until the text changes. Enter while the composer is focused still submits the typed draft, not the highlighted completion. Prefix matches rank above substring matches; producer ordering is preserved within each group, results are capped at 200, and navigation clamps rather than wraps. The list rises above the composer with theme-primary borders and explicit command/skill labels; compact widths shorten the name column to retain the label. Hidden/covered lists cannot consume their acceptance keys.

The demo catalog contains only `preview-help`, `preview-replay`, and `preview-skill`; **none dispatches a command or runs a skill**. Real candidate discovery, alias/collision deduplication, live catalog refresh and picker-specific mouse-wheel scrolling are not implemented. Left-click acceptance uses the rendered row offset, requires press/release on the same row, and is invalidated by refiltering, hiding or viewport changes. Unicode case-insensitive filtering uses the Rust compiler's lowercase tables, so cross-version Python Unicode parity is not established here (unlike the separately frozen word-editing class). The catalog input contract requires source-ordered candidates; mock tests do not establish the producer contract.

## Safety and evidence boundary

No Python child, model request, tool execution, real approval, fleet registration or application-data persistence exists in this preview. Named paths, commands, test counts and results in the conversation are **fixtures**, not work actually performed. Typed messages are kept only in memory (six turns maximum); quitting discards them. Cargo/launcher build actions are separate developer operations, not mock tool execution.

The terminal defaults to the source-derived **Ash** palette: borderless amber tool rows on black; rounded gray assistant frames; nested dashed thinking blocks. Ryan accepted this revised visual direction on 2026-10-15 after rejecting the initial generic card redesign. Preserve the layout while wiring runtime events. F4 now offers **36 public dark themes**, generated from LiteTUI and Textual definitions without reading live settings. Search by name, use arrows, then Enter to apply; Escape cancels. F5 replays the fixture without resetting the theme. Semantic tokens drive the same layout across palettes, including resolved thinking/tool/footer overrides. Legacy fixed argument/result grays remain intentional. Selection is in-memory only: real custom-theme editing, runtime catalog loading and saved selection are still required. Synthetic override tests are not custom-theme integration. The terminal uses an explicit dark palette. A cleanup guard restores raw mode, mouse capture, bracketed paste, alternate screen and cursor on normal exit or recoverable errors. Panic handling attempts restoration. Force termination cannot guarantee cleanup.

## Offline wire primitives (not connected to the preview)

The library's `protocol` module provides an incremental JSONL decoder and a connection-local negotiation/correlation client. Neither starts a process, writes a command, applies application state, resolves an approval, or authorizes a mutation.

- The decoder requires an explicit positive byte limit per frame (LF excluded; CR included). It preserves complete records delivered before a later error and stops permanently on framing failure or EOF. JSON nesting is bounded at 128 values. Raw-aware container parsing preserves opaque objects and exact numeric payloads, including integers beyond machine range; it does not turn serde's private number-map spelling into a numeric identity.
- A matching successful version-1 hello establishes compatibility only. State arriving during negotiation latches uncertainty that a later hello cannot clear. Compatibility is not snapshot continuity or authority.
- A caller-supplied, reviewed delivery policy selects mirrored envelopes or direct-only events. Unknown events and malformed envelopes fail closed; equal consecutive events are never deduplicated. Event payload schemas and the production delivery inventory are **not implemented** by this module.
- Generated string command IDs are unique within a caller-owned generation. Incoming IDs preserve string/integer distinctions; unsupported numeric IDs outside i64/u64 range fail explicitly. Pending correlation is bounded. Faulted clients can drain valid pending responses; disconnected clients retain uncertain requests and never resend.

Tests currently cover these primitives with Rust fixtures, including raw JSONL-to-client type-coercion regressions. **Rust replay of the Python-produced capture bundles remains open.** No production limits, downstream queue, child lifecycle, state reducer, or live activation path is supplied. The preview remains unchanged and offline.

## Dependencies and Ratty target

Pinned Ratatui **0.30.2**, Crossterm **0.29.0**, unicode-width **0.2.2**, unicode-segmentation **1.13.3**, serde **1.0.229**, and serde_json **1.0.151** (`arbitrary_precision` and `raw_value`); lockfile committed. Rust 1.92.0 was used to build. The package declares the dependencies' supported Rust floor (1.88), but that older compiler was not tested here.

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
