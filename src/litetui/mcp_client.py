"""MCP servers — stdio child processes AND plain-JSON HTTP endpoints.

Reads `mcp.json` from the repo root, falling back to `.mcp.json` (the Claude
Code project convention) when present:

```json
{ "mcpServers": {
    "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
    "litesuite-tools": { "type": "http", "url": "http://localhost:7423/mcp" }
} }
```

A server entry with a `url` is reached over HTTP POST — the stateless,
plain-JSON variant of MCP streamable-HTTP: one POST per request, and the
response body IS the JSON-RPC reply (no SSE framing). That is what LiteSuite's
/mcp endpoint implements. Everything else is spawned as a stdio child. Both
transports expose the same interface, so each server's tools are registered as
`mcp__<server>__<tool>` and dispatched like any other tool — the agent loop
needs no MCP-specific branch.

🔴 THE STDERR RULE IS LOAD-BEARING, NOT HYGIENE. An MCP server is a long-running
child process, and a child that inherits this process's console sprays its
output straight through the Textual screen — corrupting the display of a TUI
that owns every cell. stderr therefore goes to `mcp.log`, never to the terminal,
and stdout is consumed exclusively as JSON-RPC frames.

Blocking by design: the app dispatches tools through `asyncio.to_thread`, so a
synchronous client with one lock per server is both correct and simpler than an
async one. Every wait is bounded — a hung server must degrade to one failed tool
call, never a frozen UI.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request
from litetui import runtime_log, ttyguard
from pathlib import Path

MCP_LOG_NAME = "mcp.log"
INIT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_RESULT_CHARS = 50_000
PROTOCOL_VERSION = "2025-06-18"

#: Config files scanned at the repo root, in precedence order. `mcp.json`
#: is LiteTUI-native; `.mcp.json` is the Claude Code project convention and
#: is read when present so a repo that already ships one (e.g. to register
#: this app's own bridge for Claude sessions) gets it for free. On a name
#: collision the EARLIER file wins — see read_server_configs.
MCP_CONFIG_NAMES = ("mcp.json", ".mcp.json")


class MCPError(RuntimeError):
    pass


class MCPServer:
    """One stdio MCP server process."""

    def __init__(self, name: str, cfg: dict, cwd: Path, log_handle):
        self.name = name
        self.cfg = cfg
        self.cwd = cwd
        self._log = log_handle
        self.proc: subprocess.Popen | None = None
        self.tools: list[dict] = []
        self.error: str | None = None
        self._lock = threading.Lock()
        self._id = 0
        # Frames drained off stdout by _reader_loop. `None` is the sentinel for
        # "nothing more will arrive" (EOF or a dead reader).
        self._frames: queue.Queue = queue.Queue()
        # Responses that arrived for a DIFFERENT id than the one being awaited.
        # Bounded in practice: `call()` serialises requests per server, so only
        # frames belonging to already-abandoned requests can accumulate — and a
        # timeout kills the process, so they stop arriving.
        self._pending: dict[int, dict] = {}
        self._reader: threading.Thread | None = None

    # ── wire ────────────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError("server is not running")
        # A poisoned server (see _poison) keeps its Popen object, with a closed
        # stdin and an exit code. Writing to it would raise ValueError three
        # frames down; say what actually happened instead.
        if self.proc.poll() is not None:
            raise MCPError(f"server is not running (exit {self.proc.returncode})")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _start_reader(self) -> None:
        """Drain stdout on a thread of its own. Idempotent.

        🔴 THIS IS THE BOUND. `_read_until` used to check its deadline and then
        call `stdout.readline()` on the CALLER's thread. That read blocks, so a
        server which stays alive and emits no newline held the call forever —
        the deadline was never re-examined and neither was `poll()`. Probed
        2026-08-23: still blocked at 5s against a declared 0.1s timeout, with
        the module docstring one screen above promising "every wait is bounded".

        Moving the blocking read here is what makes the timeout real: the reader
        may block as long as it likes, because nothing waits on IT — callers
        wait on a queue, which takes a deadline.
        """
        if self._reader and self._reader.is_alive():
            return
        self._reader = threading.Thread(
            target=self._reader_loop, name=f"mcp-reader-{self.name}", daemon=True
        )
        self._reader.start()

    def _reader_loop(self) -> None:
        """Parse frames off stdout until it ends. Never raises to the caller."""
        stdout = self.proc.stdout if self.proc else None
        if stdout is None:
            self._frames.put(None)
            return
        try:
            while True:
                line = stdout.readline()
                if not line:
                    break                       # EOF — the pipe is done
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Not a frame. Servers that print banners to stdout are
                    # common and their noise must not be mistaken for a
                    # protocol error.
                    self._log.write(f"[{self.name}] non-JSON stdout: {line[:400]}\n")
                    self._log.flush()
                    continue
                self._frames.put(msg)
        except Exception as e:
            runtime_log.record(
                "mcp_reader_failed",
                site="mcp.reader",
                component="mcp",
                server=self.name,
                error_type=type(e).__name__,
            )
            self._log.write(f"[{self.name}] reader stopped: {type(e).__name__}: {e}\n")
            self._log.flush()
        finally:
            # Sentinel, in a finally: a caller blocked on the queue must be
            # released even when the reader dies of something unforeseen.
            # Without it, killing the reader would recreate the very hang this
            # whole change removes.
            self._frames.put(None)

    def _poison(self, why: str) -> None:
        """Retire a server that missed its deadline.

        ⚠️ A LATE FRAME IS WORSE THAN NO FRAME. The server may still deliver
        the response after the timeout, and the next request would then take it
        as its own answer — matching on `id` narrows that but does not close it,
        because a retried call re-asks the same question and would accept the
        stale reply. So the process goes, and the next call starts clean.
        """
        self.error = why
        try:
            self.stop()
        except Exception:
            pass

    def _read_until(self, want_id: int, timeout: float) -> dict:
        """Wait for the frame matching want_id, bounded by `timeout`.

        Notifications are skipped and responses for OTHER ids are held rather
        than dropped: the reader drains continuously now, so a frame belonging
        to another request can arrive mid-wait, and discarding it would strand
        whoever is waiting on it — a hang with no timeout attached.
        """
        if not self.proc:
            raise MCPError("server is not running")
        held = self._pending.pop(want_id, None)
        if held is not None:
            return held

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                why = f"timed out after {timeout:.0f}s waiting for response"
                self._poison(why)
                raise MCPError(why)
            # Capped so the deadline is re-examined on a regular tick rather
            # than only when a frame happens to land.
            try:
                msg = self._frames.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if msg is None:
                code = self.proc.poll() if self.proc else None
                if code is not None:
                    raise MCPError(f"server exited with code {code}")
                raise MCPError("server closed its stdout")
            mid = msg.get("id")
            if mid == want_id:
                return msg
            if mid is not None:
                self._pending[mid] = msg

    def _request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        rid = self._next_id()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        msg = self._read_until(rid, timeout)
        if "error" in msg:
            err = msg["error"]
            raise MCPError(f"{err.get('code')}: {err.get('message')}")
        return msg.get("result", {})

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> None:
        command = self.cfg.get("command")
        if not command:
            raise MCPError("no `command` in config")
        args = list(self.cfg.get("args") or [])
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        # A child that inherits the console writes over the TUI. stderr is
        # redirected for exactly that reason; see the module docstring.
        self.proc = ttyguard.popen(
            [command, *args],
            cwd=str(self.cfg.get("cwd") or self.cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            bufsize=1,
            # utf-8/replace decode and the no-console-window flag are
            # ttyguard's defaults; the envelope runs the terminal repair
            # once after the spawn and does not own the process.
        )
        # BEFORE the handshake: `initialize` waits on the same primitive every
        # tool call does, so a server that stalls during the handshake would
        # hang the app at BOOT rather than at first use.
        self._start_reader()
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "LiteTUI", "version": "1.0"},
            },
            timeout=INIT_TIMEOUT,
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self.tools = list(self._request("tools/list", timeout=INIT_TIMEOUT).get("tools") or [])

    def call(self, tool: str, args: dict) -> str:
        with self._lock:
            result = self._request("tools/call", {"name": tool, "arguments": args})
        return _flatten_content(result)

    def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class HTTPMCPServer:
    """One remote MCP server reached over plain-JSON HTTP POST.

    The stateless variant of MCP streamable-HTTP: every request is one POST,
    and the response body IS the JSON-RPC reply — no SSE framing, no session
    id, stdlib urllib only. That is what LiteSuite's /mcp endpoint implements
    (a one-shot CLI per call under the hood), so a dead or hung server degrades
    to failed tool calls exactly like the stdio transport: every wait below is
    bounded by `timeout`, and nothing here blocks without a deadline.

    Interface parity with MCPServer is deliberate, not incidental: MCPManager,
    app.py's disabled-server path (which calls srv.stop()) and the dispatch
    table all treat both transports identically. There is no process to kill —
    stop() exists so that code stays transport-blind.
    """

    def __init__(self, name: str, cfg: dict, cwd: Path, log_handle):
        self.name = name
        self.cfg = cfg
        self.cwd = cwd
        self._log = log_handle
        self.url = str(cfg.get("url") or "").strip()
        #: Extra headers (e.g. Authorization) merged over the defaults. The
        #: bridge endpoint is auth-exempt on loopback, so this is usually empty.
        self.headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        self.tools: list[dict] = []
        self.error: str | None = None
        self.proc = None  # interface parity with MCPServer; there is no process
        self._lock = threading.Lock()
        self._id = 0

    def _post(self, payload: dict, timeout: float) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.headers)
        req = urllib.request.Request(self.url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(400).decode("utf-8", "replace")
            except Exception:
                pass
            raise MCPError(f"HTTP {e.code} from {self.url}: {detail[:200]}")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", None) or str(e)
            raise MCPError(f"cannot reach {self.url}: {reason}")
        if not body:
            # A 2xx with no body is a valid notification ack (LiteSuite answers
            # notifications/* with 202 and an empty payload).
            return {}
        try:
            msg = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            raise MCPError(f"non-JSON response from {self.url} ({len(body)} bytes)")
        if not isinstance(msg, dict) or "jsonrpc" not in msg:
            raise MCPError(f"response is not a JSON-RPC message from {self.url}")
        return msg

    def _request(self, method: str, params: dict | None = None, timeout: float = CALL_TIMEOUT) -> dict:
        # The lock serializes requests per server — the same bound the stdio
        # transport gets from call() holding self._lock.
        with self._lock:
            self._id += 1
            rid = self._id
            msg = self._post(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
                timeout,
            )
        if "error" in msg:
            err = msg["error"]
            raise MCPError(f"{err.get('code')}: {err.get('message')}")
        return msg.get("result", {})

    def _notify(self, method: str) -> None:
        """Best-effort by design: a notification that fails must not sink the
        handshake. A server that rejects notifications/initialized is still
        usable for tools/call — log and move on, the same degrade-don't-freeze
        rule as the stdio reader."""
        try:
            self._post({"jsonrpc": "2.0", "method": method, "params": {}}, INIT_TIMEOUT)
        except Exception as e:
            if self._log is not None:
                self._log.write(f"[{self.name}] notification {method} failed: {e}\n")
                self._log.flush()

    def start(self) -> None:
        if not self.url:
            raise MCPError("no `url` in config")
        # BEFORE any tool call can wait on it, like the stdio handshake: a dead
        # endpoint fails at BOOT (recorded by MCPManager), not first use.
        self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "LiteTUI", "version": "1.0"},
            },
            timeout=INIT_TIMEOUT,
        )
        self._notify("notifications/initialized")
        self.tools = list(self._request("tools/list", None, INIT_TIMEOUT).get("tools") or [])

    def call(self, tool: str, args: dict) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": args})
        return _flatten_content(result)

    def stop(self) -> None:
        # No process to kill. Present for interface parity — app.py's
        # disabled-server path calls it on whatever transport came back.
        pass

def _flatten_content(result: dict) -> str:
    """MCP content blocks -> text. Non-text blocks are NAMED, never dropped."""
    if not isinstance(result, dict):
        return str(result)
    blocks = result.get("content")
    if blocks is None:
        return json.dumps(result, ensure_ascii=False, indent=2)[:MAX_RESULT_CHARS]
    parts: list[str] = []
    for b in blocks if isinstance(blocks, list) else [blocks]:
        if not isinstance(b, dict):
            parts.append(str(b))
            continue
        kind = b.get("type")
        if kind == "text":
            parts.append(b.get("text", ""))
        elif kind == "resource":
            res = b.get("resource") or {}
            parts.append(res.get("text") or f"[resource {res.get('uri', '?')}]")
        else:
            # An image or an unknown block still happened; saying so beats
            # returning an empty string that reads as "the tool did nothing".
            parts.append(f"[{kind or 'unknown'} content omitted]")
    text = "\n".join(p for p in parts if p)
    if result.get("isError"):
        text = f"[tool reported an error]\n{text}"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "\n[truncated]"
    return text or "(empty result)"


def config_files(root: Path) -> list[Path]:
    """Existing MCP config files at the repo root, in precedence order."""
    return [root / n for n in MCP_CONFIG_NAMES if (root / n).is_file()]


def read_server_configs(cfg_paths: list[Path]) -> tuple[dict[str, dict], dict[str, str]]:
    """Merge mcpServers from every existing config file.

    Returns (servers, errors): servers keyed by name with the EARLIER file
    winning a collision — mcp.json is LiteTUI-native and stays authoritative
    over the Claude-Code dotfile that happens to share the repo root; errors
    keyed by FILE NAME so an unreadable config shows up in /settings rather
    than vanishing. A file whose top level is not the expected shape is
    skipped, like an empty one: there is nothing to start from it.
    """
    merged: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for p in cfg_paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            runtime_log.record(
                "mcp_config_failed",
                site="mcp.manager.load",
                component="mcp",
                operation="config_load",
                error_type=type(e).__name__,
            )
            errors[p.name] = f"unreadable: {e}"
            continue
        block = data.get("mcpServers") or data.get("servers") or {}
        if not isinstance(block, dict):
            continue
        for name, sc in block.items():
            merged.setdefault(name, sc)
    return merged, errors

class MCPManager:
    """Loads mcp.json / .mcp.json (see config_files), starts each server —
stdio or HTTP by entry shape — and exposes specs + a dispatch map."""

    def __init__(self, root: Path):
        self.root = root
        self.servers: dict[str, "MCPServer | HTTPMCPServer"] = {}
        self.failures: dict[str, str] = {}
        self._log_handle = None

    def load(self) -> None:
        cfg_paths = config_files(self.root)
        if not cfg_paths:
            return
        servers, file_errors = read_server_configs(cfg_paths)
        self.failures.update(file_errors)
        if not servers:
            return
        self._log_handle = open(self.root / MCP_LOG_NAME, "a", encoding="utf-8", errors="replace")
        for name, sc in servers.items():
            if not isinstance(sc, dict) or sc.get("disabled"):
                continue
            # Transport by shape: a `url` without a `command` is an HTTP
            # endpoint (Claude Code's `"type": "http"` entries carry it);
            # everything else is spawned as a stdio child.
            if sc.get("url") and not sc.get("command"):
                srv = HTTPMCPServer(name, sc, self.root, self._log_handle)
            else:
                srv = MCPServer(name, sc, self.root, self._log_handle)
            try:
                srv.start()
                self.servers[name] = srv
            except Exception as e:
                runtime_log.record(
                    "mcp_server_start_failed",
                    site="mcp.manager.load",
                    component="mcp",
                    server=name,
                    operation="start",
                    error_type=type(e).__name__,
                )
                # One bad server must not cost the others, and must not be
                # silent: a tool that never appears looks like one the model
                # simply chose not to use.
                self.failures[name] = f"{type(e).__name__}: {e}"
                srv.stop()

    def tool_specs(self) -> list[dict]:
        specs: list[dict] = []
        for sname, srv in self.servers.items():
            for t in srv.tools:
                tname = t.get("name")
                if not tname:
                    continue
                schema = t.get("inputSchema") or {"type": "object", "properties": {}}
                specs.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{sname}__{tname}",
                            "description": (t.get("description") or f"{sname} MCP tool {tname}")[:1024],
                            "parameters": schema,
                        },
                    }
                )
        return specs

    def dispatch(self) -> dict:
        table = {}
        for sname, srv in self.servers.items():
            for t in srv.tools:
                tname = t.get("name")
                if not tname:
                    continue

                def _make(_srv=srv, _tool=tname):
                    def _call(args: dict) -> str:
                        try:
                            return _srv.call(_tool, args)
                        except Exception as e:
                            runtime_log.record(
                                "mcp_tool_failed",
                                site="mcp.dispatch",
                                component="mcp",
                                server=_srv.name,
                                name=_tool,
                                error_type=type(e).__name__,
                            )
                            return f"[error] mcp {_srv.name}/{_tool}: {type(e).__name__}: {e}"

                    return _call

                table[f"mcp__{sname}__{tname}"] = _make()
        return table

    def status_line(self) -> str:
        bits = [f"{n}:{len(s.tools)}" for n, s in self.servers.items()]
        for n, err in self.failures.items():
            bits.append(f"{n}:FAILED")
        return " · ".join(bits)

    def stop_all(self) -> None:
        for s in self.servers.values():
            s.stop()
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
