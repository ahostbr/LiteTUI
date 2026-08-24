"""MCP over stdio — reads a standard `mcp.json` from the repo root.

```json
{ "mcpServers": {
    "files": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] }
} }
```

Each server's tools are registered as `mcp__<server>__<tool>` and dispatched
like any other tool, so the agent loop needs no MCP-specific branch.

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
from litetui import runtime_log, ttyguard
from pathlib import Path

MCP_CONFIG_NAME = "mcp.json"
MCP_LOG_NAME = "mcp.log"
INIT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_RESULT_CHARS = 50_000
PROTOCOL_VERSION = "2025-06-18"


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


class MCPManager:
    """Loads mcp.json, starts each server, exposes specs + a dispatch map."""

    def __init__(self, root: Path):
        self.root = root
        self.servers: dict[str, MCPServer] = {}
        self.failures: dict[str, str] = {}
        self._log_handle = None

    def load(self) -> None:
        cfg_path = self.root / MCP_CONFIG_NAME
        if not cfg_path.is_file():
            return
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:
            runtime_log.record(
                "mcp_config_failed",
                site="mcp.manager.load",
                component="mcp",
                operation="config_load",
                error_type=type(e).__name__,
            )
            self.failures["mcp.json"] = f"unreadable: {e}"
            return
        servers = cfg.get("mcpServers") or cfg.get("servers") or {}
        if not isinstance(servers, dict) or not servers:
            return
        self._log_handle = open(self.root / MCP_LOG_NAME, "a", encoding="utf-8", errors="replace")
        for name, sc in servers.items():
            if not isinstance(sc, dict) or sc.get("disabled"):
                continue
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
