# LiteTUI

A terminal chat client and agent harness for a **local** LLM served by
[LM Studio](https://lmstudio.ai). Textual TUI, streaming, tool use, vision,
per-conversation memory, and compaction.

Built against `qwen3.8-27b` on an RTX 5090. Nothing here talks to a hosted API.

```bash
uv sync
run.bat            # or: python app.py
```

Expects LM Studio's server on `http://localhost:1234/v1` with a model loaded.

## What it does

**Chat + agent loop.** Streams replies, renders the thinking trace in a
collapsible block, and runs a tool loop until the model gives a plain answer.

**Four tools**, pi-style, capped at 2000 lines / 50KB per result:
`bash` · `read` · `write` · `web_fetch`. Toggle with `Ctrl+T`.

**Vision.** Paste an image with `Ctrl+O`, or give it a path — quoted or bare,
with or without a question after it:

```
C:\shots\screen.png what is the error in this dialog?
"C:\My Folder\screen.png"
```

> A model can only see images if LM Studio reports it as `type: vlm`. A GGUF
> shipped without an `mmproj` projector loads as `type: llm` and returns
> HTTP 400 on image input — the fix is to place a matching `mmproj-*.gguf`
> beside the weights, not to change anything here.

## Conversations

Every conversation gets a directory:

```
.convos/<uuid>/
    convo.jsonl    append-only transcript
    memory.md      an INDEX the agent maintains
    soul.md        who it is here: preferences, standing corrections
    handoff.md     in flight / owed / absent-by-decision / caveats
    memories/      the memories themselves, one file per idea
```

The agent is told its own uuid and absolute path in the system prompt, and
`memory.md`, `soul.md` and `handoff.md` are **injected into every prompt**,
re-read from disk each turn. A file it writes on one turn is visible on the
next. Index lines are capped at ~50 tokens: pointers, never the memory itself.

### The transcript is append-only, always

`convo.jsonl` is never rewritten and never backed up and replaced. A torn
append costs one line, which the reader tolerates; a failed rewrite costs the
conversation. Record types applied in file order:

| type | effect |
|---|---|
| `meta` | id / created / model |
| `msg` | one message, verbatim |
| `edit` | replace ONE message in place |
| `truncate` | drop a head range, splice new messages in front |
| `snapshot` | whole list (read for older files; no longer written) |

`/compact` records a `truncate` rather than a fresh copy of everything.
Measured on a 30-message conversation: **303 bytes instead of 5,445** — and the
raw history stays on disk behind the marker.

## Commands

| | |
|---|---|
| `/new` `/clear` | start a new conversation (new folder on disk) |
| `/system <text>` | set the system prompt |
| `/model [n]` | show or switch model |
| `/think [level]` | `off · minimal · low · medium · high · xhigh · unset` |
| `/compact [hint]` | summarise older messages, keep the last 4 |
| `/convos` | list saved conversations with sizes |
| `/resume <n\|id>` | load one (id = uuid prefix) |
| `/reconnect` `/quit` | |

`Esc` stops the current turn (asks first; `Esc` again forces). `Ctrl+O` paste
image · `Ctrl+X` clear image · `Ctrl+L` new conversation · `Ctrl+T` tools.

### `/think` — unset is not off

`unset` sends no `reasoning_effort` at all, and **LM Studio's own default for an
absent value is `xhigh`**. So "unset" means maximum thinking, not none.
`/think off` is a different thing and sends `reasoning_effort: "none"`.

The six accepted values come from the server's own 400 body, not its docs,
which omit the parameter entirely:

```
Invalid 'reasoning_effort' value: 'x'.
Supported values: none, minimal, low, medium, high, xhigh.
```

### `/compact` writes to memory before it summarises

Compaction is the moment context is about to be destroyed, so the compaction
prompt runs in two steps: **persist first** (durable lesson → a new file in
`memories/` plus one pointer line; a correction → `soul.md`; anything in flight
→ `handoff.md`), **then** produce the summary. Tools are passed to that call so
the instruction can actually reach disk — without them it would be theatre.

It reports which of the three happened, including
`persisted: nothing — TOOLS ARE OFF, so it could not write`.

The summarisation call forces `reasoning_effort: "none"` regardless of
`/think`: at the server default a 12k budget was spent entirely on the thinking
trace and returned an **empty** answer, and a compaction that returns nothing is
worse than not compacting.

## Tests

```bash
python test_convos.py     # persistence, tool-pairing safety, helpers
```

No network, no TUI, no LM Studio required.

## Bundled

`chrome-bridge/` — drive Chrome from Python (navigate, read page, click,
screenshot) via an MV3 extension. `pccontrol/` — Win32 mouse/keyboard/window
automation and OCR helpers. Both are separate tools that happen to live here;
`chrome-bridge` also has a canonical copy elsewhere, so treat this one as a
snapshot rather than the source of truth.
