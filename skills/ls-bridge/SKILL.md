---
name: ls-bridge
description: Drive LiteSuite via the Agent Bridge (:7423) — ping, PTY list/read/talk, browser panes (navigate/screenshot/click/execute-js), open editor/media panels. Triggers on 'agent bridge', 'bridge'.
---

# LiteSuite Agent Bridge

You are running inside a LiteSuite terminal pane. The app exposes an HTTP
REST API at `127.0.0.1:7423` (the **Agent Bridge**) that can see and drive
every pane on the canvas — terminals, browsers, editor, media. Use it instead
of guessing what is on screen or reaching for desktop automation when a route
exists.

Source of truth: `C:\Projects\LiteSuite\apps\desktop\src\litesuite\services\agent-bridge.ts`
— if a call misbehaves, read the handler before retrying blindly.

## Auth (every request)

Bearer token from `~/.litesuite/bridge-token` — 64 hex chars, **regenerated on
every app launch** and deleted on shutdown. Never hardcode it; read it fresh:

```powershell
$t = (Get-Content $env:USERPROFILE\.litesuite\bridge-token).Trim()
$H = @{ Authorization = "Bearer $t" }
```

Status-code semantics: **401** = no/bad token (app is up), **404** = auth OK,
no such route. There is **no `/health` endpoint** — the liveness ping is:

```powershell
Invoke-WebRequest -Uri 'http://127.0.0.1:7423/pty/list' -Headers $H -UseBasicParsing
```

A 200 with a session list proves the bridge, the app, and your own terminal
(you'll see your pane in it) are all alive.

## Terminals (PTYs)

| Route | Method | Body / notes |
| --- | --- | --- |
| `/pty/list` | GET | `{sessions:[{id,pid,shell,cwd,...}]}` — ids look like `pty-1-<ts>` |
| `/pty/read` | POST | `{session_id}` → `{output}` (buffer text) |
| `/pty/write` | POST | `{session_id, data}` — raw bytes, no newline added; focuses the pane first |
| `/pty/talk` | POST | `{session_id, command}` — writes `command + "\r"` (i.e. runs it) |
| `/pty/create` | POST | `{shell, cwd?, env?}` → new terminal pane + session id |
| `DELETE /pty/<id>` | DELETE | kills the session and reaps its pane |

## Browser panes

Addressing: every route takes `session_id`, which may be a raw browserManager
id (from `/browser/list`), **or** a canvas pane id, or an alias — `self`,
`self:<agentId>`, `sentinel`.

| Route | Method | Body / notes |
| --- | --- | --- |
| `/browser/list` | GET | `{sessions:[ids]}` — ids only, no URLs |
| `/browser/create` | POST | new browser pane/session |
| `/browser/navigate` | POST | `{session_id, url}` (bare host gets `https://` prefixed) |
| `/browser/go-back`, `/go-forward`, `/reload` | POST | `{session_id}` |
| `/browser/read-page` | POST | DOM index of clickable/typable elements — use this to find a target's **index** |
| `/browser/click` | POST | `{session_id, index}` (index from read-page) |
| `/browser/type` | POST | `{session_id, text, index?}` |
| `/browser/scroll` | POST | `{session_id, direction: up\|down\|left\|right, amount?}` |
| `/browser/select-option` | POST | `{session_id, element_index, option_index}` |
| `/browser/screenshot` | POST | `{session_id}` → `{success, dataUrl, width, height}` |
| `/browser/execute-js` | POST | `{session_id, code}` → runs in the page; great for `location.href`, `document.title` |
| `/browser/console-logs` | POST | `{session_id, since?}` |

**The screenshot loop (do all three steps):**

```powershell
$body = @{ session_id = 'browser-2-1788316550528' } | ConvertTo-Json
$r = Invoke-WebRequest -Uri 'http://127.0.0.1:7423/browser/screenshot' -Method POST `
      -Headers @{ Authorization = "Bearer $t"; 'Content-Type' = 'application/json' } `
      -Body $body -UseBasicParsing
$j = $r.Content | ConvertFrom-Json
$b64 = $j.dataUrl.Substring(22)   # strip "data:image/png;base64," (exactly 22 chars)
[IO.File]::WriteAllBytes('C:\Projects\LiteTUI\temp-working-dir\shot.png', [Convert]::FromBase64String($b64))
```

Then **open the file with `view_image`** — a screenshot you never look at is a
file, not an observation.

🔴 **Empty capture ≠ broken.** Paint follows viewport visibility on the
infinite canvas: if the pane is offscreen the PNG comes back empty and the
error tells you so. Fix: `POST /canvas/focus-pane {"paneId": "<id>"}`, then
retry. If it's not a pane address, fall back to `/browser/read-page`.

## Opening panels (the "open X in LiteSuite" routes)

| Route | Method | Body | Result |
| --- | --- | --- | --- |
| `/canvas/terminal` | POST | `{title?, cwd?}` | new terminal pane → `paneId` |
| `/canvas/browser` | POST | `{url, title?, paneId?}` — with `paneId` it navigates THAT pane instead of minting one | browser pane → `paneId` |
| `/canvas/editor` | POST | `{filePath}` (absolute) | opens the editor panel on that file |
| `/editor/open` | POST | `{filePath}` | same as above, older alias — works identically |
| `/canvas/media` | POST | `{path}` or `{url}`, `title?`, `kind?` | media pane (local files streamed via GenUI server) |
| `/canvas/claude` | POST | `{cwd?, model?, permissionMode?}` | spawns a Claude terminal pane, returns session id/pid |
| `/canvas/focus-pane` | POST | `{paneId}` | brings an offscreen pane into view |
| `/canvas/remove-pane`, `/maximize`, `/unmaximize`, `/move-pane`, `/split`, `/tab`, `/grid`, `/split-grid` | POST | see source | canvas layout ops |

New panes land right of the last pane and are auto-focused unless you pass
`"focus": false`. After any panel-open, **verify with a desktop screenshot**
(`pccontrol(action="screenshot", monitor=0)` → `view_image`) — `{ok:true}` is
the IPC send succeeding, not proof the pane rendered.

## Also on the bridge

- `POST /shell/execute {command}` — runs through just-bash (needs a workspace
  open; 503 otherwise). Prefer your own shell tool for one-offs.
- `POST /mcp` — JSON-RPC 2.0 MCP endpoint, **no auth needed**. Full section below.
- `/session/list|register|resolve`, `/context`, `/credit-balance`,
  `/api/annotations`, `/v1/sentinel/assistant-message`, `/v1/image/generate`,
  `/ui/render`, `/evolution/*` — read the source for bodies; these are niche.

## MCP endpoint (`POST /mcp`) — no token required

The same port also speaks **JSON-RPC 2.0 MCP** (server `litesuite-tools`
v0.1.0) — the surface LiteSuite's own Claude sessions connect to as an MCP
server. It exposes 35 tools covering the whole app: terminals, browser,
editor, image/sound/3D generation, kanban tasks, memory, vault, widgets…

- **Auth-exempt on loopback.** No bearer token (the app's MCP wizard registers
  a bare URL with no headers). Defenses instead of auth: any `Origin` header
  is rejected and `Content-Type: application/json` is required — the drive-by
  form-POST RCE countermeasure. Don't send an Origin header from your scripts.
- Methods: `initialize`, `ping`, `tools/list`, `tools/call`;
  `notifications/*` → 202; `resources/*` / `prompts/*` are empty stubs.

```powershell
$H2 = @{ 'Content-Type'='application/json' }   # NO Authorization header

# ping — liveness check that needs no token at all
Invoke-WebRequest -Uri 'http://127.0.0.1:7423/mcp' -Method POST -Headers $H2 `
  -Body '{"jsonrpc":"2.0","id":1,"method":"ping"}' -UseBasicParsing

# tools/list — the full manifest (~26KB, 35 tools); save it for reference
$body = '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
(Invoke-WebRequest -Uri 'http://127.0.0.1:7423/mcp' -Method POST -Headers $H2 `
  -Body $body -UseBasicParsing).Content | Out-File mcp-tools.json

# tools/call — run one tool; stdout comes back in result.content[0].text
$body = '{"jsonrpc":"2.0","id":2,"method":"tools/call",
         "params":{"name":"credit","arguments":{"action":"balance"}}}'
$r = Invoke-WebRequest -Uri 'http://127.0.0.1:7423/mcp' -Method POST `
      -Headers $H2 -Body $body -UseBasicParsing
($r.Content | ConvertFrom-Json).result.content[0].text   # e.g. {"balance":null}
```

Response shape (verified): `{"jsonrpc":"2.0","id":N,"result":{"content":[{"type":"text","text":"<tool stdout>"}]}}`.
Under the hood each `tools/call` runs `python -m litesuite_tools.cli run <name> --json-input '<json>'` as a one-shot process (120 s timeout) — every call is a fresh CLI, not a long-lived server.

### The 35 tools (verified via tools/list this session)

| Tool | Actions / notes |
| --- | --- |
| `agent` | LiteAgent status, activity, identity, heartbeat, TTS |
| `bench` | LiteBench LLM benchmark studio — endpoints, suites, cases, runs, compare |
| `browser` | create, navigate, click, type, screenshot, read page |
| `chronicle` | personal career timeline / milestone tracker |
| `credit` | API credit balance → observed `{"balance":null}` (not a bug) |
| `editor` | PTY sessions, browser panel control, file open |
| `environment` | `get`, `help` — project/git/system context; cwd is the Electron install dir |
| `evolution` | autonomous mutation / benchmarking / self-improvement engine (has `dry_run`) |
| `file_io` | read / write / list with path containment |
| `halt` | `halt`, `resume`, `status` — pause orchestration for human review |
| `image` | LiteImage generate, load/unload models, status |
| `inbox` | `send`, `read`, `list`, `discover` — LiteHarness maildir inter-agent messaging |
| `inject` | inject a task into the running orchestration session |
| `lens` | VRAM-aware local LLM summarization & extraction via LM Studio |
| `memory` | working memory: goal/blocker/step tracking + FTS5 search (`get project_id=default`) |
| `model` | LiteModeler prompt/image→3D GLB, batch, auto-rig, skeleton/format export |
| `pattern` | `record`, `query`, `list`, `verify`, `revoke` — success/failure patterns (FTS5) |
| `pccontrol` | **DANGER: full desktop access** — click/type/launch/windows; armed-flag gated |
| `project_state` | `summary`, `projects`, `threads`, `thread`, `layouts`, `explain` |
| `prompt_widget` | ask the user via inline widget, blocks until response (no `action` param) |
| `rag` | `query`, `index`, `status` — FTS5+BM25 over harness patterns and code |
| `reassign` | reassign an agent to a different task |
| `render_widget` | inline widget: catalog / html / specs bands; `interactive=true` blocks (no `action` param) |
| `repo_intel` | find-symbol, find-references, find-component, route-map, todo-scan, dep-graph (+status/rebuild) |
| `sandbox` | isolated code execution in a subprocess → stdout/stderr |
| `shell` | just-bash runtime with injected credentials (no `action`: command/cwd/env/timeout) |
| `sound` | LiteSound generate music/song/sfx/ambient, poll jobs, manage backends |
| `spawn` | `pty`, `terminal`, `claude`, `split`, `list`, `kill` — spawn Claude Code sessions |
| `tasks` | kanban board (SQLite WAL): `list`, `claim`, `complete`, `unclaim`, `create`, `update`, `heartbeat`, `sweep` |
| `terminal` | PTY bridge: register, read, write, submit, ctrl, create, destroy, notify, list |
| `ui_render` | render UI surfaces for the operator (modal, browser page, inline card, toast) |
| `vault` | Obsidian vault: read / write / search / list / daily / RAG |
| `web_fetch` | fetch URL → extract text |
| `web_search` | DuckDuckGo search — no API key required |
| `youtube` | YouTube transcript extraction + video metadata |

Live-verified this session: `environment get`, `credit balance`, `tasks list`
(live fleet board, hundreds of tasks), `memory get project_id=default`.


## Gotchas learned the hard way

1. **Token is per-launch.** A token from a previous app session is dead on
   restart → you'll get 401 and waste time debugging routes. Re-read the file.
2. **`/browser/list` gives ids, not URLs.** To see what's open: `execute-js`
   with `JSON.stringify({url: location.href, title: document.title})`.
3. **PowerShell JSON bodies:** build with `@{...} | ConvertTo-Json` and set
   `'Content-Type'='application/json'`, or the bridge's body reader gets a
   string where it expects an object.
4. **`/pty/talk` appends `\r`.** For PowerShell that runs the command; don't
   double up newlines in `data`.
5. **The tab title count ≠ bell badge.** A YouTube tab titled `(30) YouTube`
   with a `9+` bell means unread notifications exist behind the panel — check
   both before reporting "nothing new".
