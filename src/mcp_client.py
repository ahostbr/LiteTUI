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
import subprocess
import threading
import time
import ttyguard
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

    # ── wire ────────────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _send(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError("server is not running")
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_until(self, want_id: int, timeout: float) -> dict:
        """Read frames until the one matching want_id. Notifications are skipped.

        A server may interleave its own notifications with responses, so
        matching on `id` is required — taking the next line would eventually
        pair a response with the wrong request, which is worse than a timeout
        because it succeeds.
        """
        if not self.proc or not self.proc.stdout:
            raise MCPError("server is not running")
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise MCPError(f"timed out after {timeout:.0f}s waiting for response")
            if self.proc.poll() is not None:
                raise MCPError(f"server exited with code {self.proc.returncode}")
            line = self.proc.stdout.readline()
            if not line:
                raise MCPError("server closed its stdout")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                # Not a frame. Servers that print banners to stdout are common
                # and their noise must not be mistaken for a protocol error.
                self._log.write(f"[{self.name}] non-JSON stdout: {line[:400]}\n")
                self._log.flush()
                continue
            if msg.get("id") == want_id:
                return msg

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
