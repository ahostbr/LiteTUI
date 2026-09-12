# LiteTUI

A terminal chat client and agent harness for **local** LLMs. Textual TUI,
streaming, tool use, vision, per-conversation memory, and compaction.

**Two engines, one seam** (`src/litetui/llm_backend.py`): [LM Studio](https://lmstudio.ai)'s
desktop server, or LiteTUI's own `llama-server` from llama.cpp. Switch between
them mid-conversation with `/backend` — the history survives.

Originally built against `qwen3.8-27b` on an RTX 5090. LM Studio and llama.cpp run locally; the optional Codex OAuth backend sends inference requests to the hosted Codex service using subscription credentials.

```bash
uv sync
run.bat            # or: uv run --locked litetui
```

Either engine will do. When both are detected, a one-time picker runs at first
boot; `/backend` changes the answer later.

| engine | where | notes |
|---|---|---|
| `lmstudio` | `http://localhost:1234` | Needs LM Studio's server up. Control (load / **unload** / context length) goes through the official `lmstudio` SDK, not a `lms` shell-out. |
| `llamacpp` | `http://localhost:7470` | LiteTUI's own `llama-server` in **router mode**: one process, every chat GGUF on the box behind it. Attaches to LiteSuite's server on `:8088` when that is healthy, otherwise spawns its own — detached, console-safe, with a log file, and killed with the app. |

Neither engine loads anything at boot, and the router runs `--models-max 2` so a
model switch cannot quietly fill 32 GB of VRAM. The router's model list is
generated: LiteSuite's install dir, LM Studio's dirs, the HuggingFace cache and
any custom roots, deduplicated, with voice and embedding GGUFs filtered out by
the file's own `general.architecture` header rather than by guessing from names.

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
`memory.md`, `soul.md` and `handoff.md` are injected **once**, into the system
message at the start of the conversation — a snapshot, not a live view. They
are not re-sent each turn: three files on every request is affordable at 1M
context and is not on a local 27B, where it crowds out the conversation. To
see current contents the agent reads them with the `read` tool, and the prompt
says so. Index lines are capped at ~50 tokens: pointers, never the memory itself.

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
| `/backend` | switch engine — LM Studio or llama.cpp. The conversation survives |
| `/load` `/unload` | put a model into memory, or free it |
| `/modelcfg` | per-model Info / Load / Inference screen (see below) |
| `/think [level]` | `off · minimal · low · medium · high · xhigh · unset` |
| `/compact [hint]` | summarise older messages, keep the last 4 |
| `/convos` | list saved conversations with sizes |
| `/resume <n\|id>` | load one (id = uuid prefix) |
| `/reconnect` `/quit` | |

`Esc` stops the current turn (asks first; `Esc` again forces). `Ctrl+O` paste
image · `Ctrl+X` clear image · `Ctrl+L` new conversation · `Ctrl+T` tools.

### `/modelcfg` — a control an engine cannot drive is greyed, never hidden

Three tabs per model: **Info**, **Load**, **Inference**. Load covers context
length, GPU offload, threads, batch sizes, parallel slots, flash attention, KV
cache quantization, mlock/mmap, RoPE, seed, a draft model for speculative
decoding, a vision `mmproj`, and the chat template. Inference layers per-model
sampling over your global `/settings`, plus structured output (a JSON schema)
and named presets.

The two engines do not expose the same knobs. **A control the active engine
cannot drive renders greyed with the reason attached** — it is never silently
absent and never a switch that does nothing. That distinction is the point: a
missing control is a question, and a fake one is a bug you find much later.

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

## Skills

`skills/<name>/SKILL.md`, same shape Claude Code uses — YAML frontmatter with
`name` and `description`, then the body.

Only the **index** (name + one line each) goes into the system prompt. The body
loads on demand through the `skill` tool. Inlining every body would spend the
context window on instructions the model doesn't need this turn; an index with
no way to open it would be worse.

A directory with no `SKILL.md` is skipped. One that can't be read becomes a
skill whose description says so — a skill that vanishes on a decode error looks
exactly like one that was never written.

## MCP

A standard `mcp.json` in the repo root; `.mcp.json` (the Claude Code project
convention) is read too when present, with `mcp.json` winning a name collision:

```json
{ "mcpServers": {
    "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
    "litesuite-tools": { "type": "http", "url": "http://localhost:7423/mcp" }
} }
```

An entry with a `url` and no `command` is reached over plain-JSON HTTP POST —
one request per call, the response body IS the JSON-RPC reply (the stateless
variant of MCP streamable-HTTP; what LiteSuite's `/mcp` endpoint implements).
Everything else is spawned as a stdio child.

Each server's tools are registered as `mcp__<server>__<tool>` and dispatched
like any other tool, so the agent loop has no MCP-specific branch. `disabled:
true` skips one.

A server that fails to start is **recorded, not swallowed** — the others still
load. A tool that silently never appears looks like one the model chose not to
use.

> Server stderr goes to `mcp.log`, never to the terminal. An MCP server is a
> long-running child process, and a child that inherits this console paints
> straight over a TUI that owns every cell.

## Two instances at once

**Running two LiteTUI windows against one model server is supported, and they run
in parallel.** llama.cpp and LM Studio both serve concurrent requests; two
instances on the *same loaded model* cost one set of weights and do not wait on
each other.

### The one thing you will be asked about: a *different* model

Loading a **different** model in a second instance puts a **second set of weights
in VRAM**, and on a card that was already full that is an OOM. So whenever
another LiteTUI is running, a load that would add a model you do not already have
resident stops and asks first:

> Another LiteTUI instance is running (OpenBolt).
> Loading a different model puts a second model in VRAM and can OOM depending on
> your setup. Load qwen/qwen3-8b anyway? **[Load] [Cancel]**

- It asks **every time**, for as long as the instance is alive. There is no
  "don't ask again" — the cost is real every time, not just the first.
- It asks on **every** route into a load: `/model`, the picker, a `/modelcfg`
  context change (a reload *is* a load), the pre-turn load, and the rpc
  `set_model` a host drives. The check lives inside the backend, so a route
  added later is covered without anyone remembering to add it.
- The **same** model raises nothing. That is the parallel case, and it is the
  point.
- A **headless** child (LiteSuite's rpc LiteTUI) cannot show a modal, so it
  refuses instead of deciding for you, and the host renders the refusal.

"Another instance" means a LiteHarness registry row with `cli = litetui` and a
live session — which includes LiteSuite's headless children, because they load
models too.

### Each conversation remembers its own setup

A conversation carries its own `.convos/<id>/settings.json`: backend, model,
thinking level (and the codex reasoning effort, which is a separate vocabulary),
the llama.cpp or LM Studio load settings for *its* model, and the seat that owned
it. Switching model or engine inside one conversation changes that conversation
only — the other one you have open does not move, and the global `settings.json`
keeps being the **defaults** a new conversation is born from, plus the app-wide
knobs (theme, seat name, dialog style).

Opening or `/resume`-ing a conversation puts it back on what it was using. If
that model or engine is no longer available here, it falls back to the default
and **says so** rather than answering quietly as something else.

### The shared `settings.json`

Two processes from one checkout share one data root and therefore one
`settings.json` — 70 keys, one file. A save reads it, writes back only the keys
*this* instance changed, and replaces it atomically, so the think level you set
in one window survives a theme change in the other. Env-sourced fields
(`LITETUI_MODEL`, `LITETUI_THINKING`, `LITETUI_BACKEND`, …) are written on every
save: the file records what you *chose*, so unsetting a variable must not
silently revert the knob.

⚠️ **`background-tasks.json` is not merged this way yet.** It is a list store, and
two instances editing tasks can still drop each other's rows (measured: A's row
gone after B saves). Merging it needs a rule for deletion that a plain
read-merge-write cannot give — a removed row would be resurrected from disk — so
it is its own change, not a line here.

⚠️ **The model ceiling is shared too.** The llama.cpp router holds at most
`--models-max` models and that limit belongs to whichever instance started it, so
a load can push out a model the other window is mid-turn on. The instance doing
the loading names what is resident before it displaces anything.

### Or keep them completely separate

Give one its own root before it starts:

```bash
LITETUI_DATA_ROOT=~/.litetui-b litetui
```

That instance gets its own `settings.json`, `.convos/`, tasks and jobs — and,
being a separate data root, no shared anything. Finer knobs exist for one field
at a time: `LITETUI_SEAT_NAME`, `LITETUI_MODEL`, `LITETUI_THINKING`,
`LITETUI_BACKEND`.

## Harness seat

LiteTUI registers as a LiteHarness agent and monitors its own inbox, so other
agents can reach it and mail **wakes** it — the message is delivered as a turn.

It does **not** run `liteharness.hooks watch` or `check_inbox`. Both are
consumers that move files out of `inbox/new/` for whichever agent id they
resolve, and a second consumer on a shared mailbox is the defect the
`ls-liteharness` fix retracted — mail vanished for three hours. `watch_inbox`
also writes to stdout, which a Textual app cannot survive.

Instead it reads the maildir directly under one rule:

> **Only ever touch a file whose `to` is this agent.**

Anything addressed elsewhere is left in `new/` exactly as found — unread,
unmoved, unclaimed. Expired messages (past `ttl_minutes`) are cleared without
delivery; the agent's own echo is skipped.

## Tests

```bash
uv run --locked python tests/run_all.py    # 138 files. THE gate — read its exit code
uv run --locked pytest -q                  # the 125 pytest-style files only
```

**Run `run_all.py`, and believe its exit code rather than a pass count.**
`tests/` holds two mutually hostile styles: pytest-style files, and script-style
files whose module body ends in `sys.exit(...)`. A module-level exit fires during
pytest *collection*, so `pytest -q` cannot collect the script-style half at all —
it reports a confident green while measuring 125 of 138 files. `run_all.py` runs
each half with the runner it needs and is the only thing that sees all of them.

No network, no TUI, and neither engine required.

## Bundled

`chrome-bridge/` — drive Chrome from Python (navigate, read page, click,
screenshot) via an MV3 extension. `pccontrol/` — Win32 mouse/keyboard/window
automation and OCR helpers. Both are separate tools that happen to live here;
`chrome-bridge` also has a canonical copy elsewhere, so treat this one as a
snapshot rather than the source of truth.
