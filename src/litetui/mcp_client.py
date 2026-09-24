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
#: A stdin write blocks when the child stops reading and its pipe buffer fills.
#: The write runs on a joinable thread joined for this many seconds. The caller's
#: TRUE worst case is THIS PLUS the teardown that follows on timeout (a child that
#: is not reading must be terminated and reaped, which takes up to _KILL_TOTAL) --
#: so a call is bounded by SEND_TIMEOUT + _KILL_TOTAL, NOT SEND_TIMEOUT alone.
SEND_TIMEOUT = 10.0
#: Teardown budgets. terminate->wait is the clean path; kill->wait is the
#: fallback. Named so the honest caller bounds above are computable, not magic.
_TERMINATE_WAIT = 5.0
_KILL_WAIT = 5.0
#: Worst-case time _kill_child can spend (clean terminate-wait OR the
#: terminate-raise-then-kill-wait path). This is what a timed-out send ADDS on
#: top of SEND_TIMEOUT.
_KILL_TOTAL = _TERMINATE_WAIT + _KILL_WAIT
#: Bounded join for the reader on stop() so a wedged reader cannot hang teardown.
_READER_JOIN = 2.0
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
        # A stdin writer that timed out and could NOT be cancelled. Retained
        # (never joined, never its stdin closed under it) until observed
        # terminal; while it lives, new sends/reconnects are refused so blocked
        # daemon writers cannot pile up. See _send / _writer_in_flight.
        self._writer: threading.Thread | None = None
        # Serialises the ADMISSION of a send (decide "no writer in flight", reap
        # a finished retained writer, and register THIS send's writer) so the
        # register->start window is never observable to a racer. Admission never
        # polls is_alive() for the admit decision; it only reads/writes
        # self._writer under this lock. See _send.
        self._send_lock = threading.Lock()
        #: Set on a send-timeout (and on a poisoned server) and cleared only by a
        # CONFIRMED stop. While set, _send and start() refuse regardless of the
        # writer's state, so a server whose cleanup is uncertain can neither be
        # written to nor restarted over. See _send / stop / _poison.
        self._quarantined: str | None = None

    # ── wire ────────────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _writer_in_flight(self) -> bool:
        """True while a writer is admitted and not yet observed terminal.

        Reaps (clears) a retained writer that has since finished. While one is
        in flight, new sends and reconnects are refused so uncancellable daemon
        writers cannot pile up. The actual ADMISSION decision is made under
        _send_lock in _send; this is the read-only view (also used by stop /
        start)."""
        with self._send_lock:
            w = self._writer
            if w is not None and not w.is_alive():
                self._writer = None          # observed terminal -> reap the retention
            return self._writer is not None

    def _send(self, payload: dict, *, timeout: float = SEND_TIMEOUT) -> None:
        # ADMISSION is decided and registered ATOMICALLY under _send_lock: refuse
        # a live writer, refuse while quarantined, reap a finished retained
        # writer, capture proc/stdin, and register THIS send's writer all in one
        # critical section. A racer can never observe the register->start window,
        # so it is refused (live writer / quarantine) rather than admitted on a
        # stale is_alive()==False. The caller's wait is bounded by `timeout` PLUS
        # the kill's bounded waits (see the SEND_TIMEOUT note) — not `timeout`
        # alone.
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._send_lock:
            w = self._writer
            if w is not None and w.is_alive():
                raise MCPError("a previous send is still blocked; server retained")
            if self._quarantined is not None:
                # A previous send timed out and its stop was never confirmed.
                # Refuse even if the writer has since finished: a clean, verified
                # stop() is what clears the quarantine (persistent quarantine).
                raise MCPError(
                    f"quarantined ({self._quarantined}); a confirmed stop is required"
                )
            if w is not None:
                self._writer = None          # terminal retained writer -> reap before admit
            # Capture proc/stdin ONCE. A reconnect can replace self.proc while
            # this write is in flight; the writer and the timeout teardown must
            # act on the process we are writing to, never late-bind a replacement.
            proc = self.proc
            if not proc or not proc.stdin:
                raise MCPError("server is not running")
            # A poisoned server keeps its Popen object, with a closed stdin and
            # an exit code. Writing would raise three frames down; say what
            # happened.
            if proc.poll() is not None:
                raise MCPError(f"server is not running (exit {proc.returncode})")
            stdin = proc.stdin

            err: dict = {}

            def _write() -> None:
                try:
                    stdin.write(line)
                    stdin.flush()
                except Exception as e:  # noqa: BLE001 — pipe closed under us / broken
                    err["e"] = e

            # 🔴 BOUNDED FOR THE CALLER, RETAINED FOR THE WRITER. write()/flush()
            # block when the child stops reading and its stdin pipe buffer fills.
            # Running them on a daemon thread joined for `timeout` bounds THIS call;
            # the write itself has NO safe cancel implemented here (OS APIs exist).
            # On timeout we kill the child's READ end to encourage a BrokenPipeError
            # — but a descendant that inherited the read end can keep it open, so we
            # do NOT assume the writer exits: it is RETAINED (never joined, its stdin
            # never closed under it) until a later call observes the thread terminal.
            t = threading.Thread(target=_write, name=f"mcp-writer-{self.name}", daemon=True)
            self._writer = t
            t.start()
        t.join(timeout)
        if t.is_alive():
            # Timeout: the write is bounded (we return) but the writer is
            # UNCANCELLABLE. Retain it (self._writer is still = t), quarantine
            # the server (refuse reuse until a confirmed stop), and kill the
            # CAPTURED proc — never self.proc, which a reconnect may have
            # replaced.
            why = f"send timed out after {timeout:.0f}s (server not reading stdin)"
            self.error = why
            with self._send_lock:
                self._quarantined = why
            self._kill_child(proc)
            raise MCPError("send timed out; server retained")
        with self._send_lock:
            if self._writer is t:
                self._writer = None              # writer terminal, retention cleared
        if "e" in err:
            raise MCPError(f"send failed ({type(err['e']).__name__})")

    def _kill_child(self, proc) -> bool:
        """Kill the CAPTURED child WITHOUT touching stdin — a retained writer may
        hold the stdin lock, so closing it here would deadlock. terminate ->
        bounded wait -> kill -> bounded wait so the process is reaped, not left a
        zombie. No raw output (type-only elsewhere).

        Returns whether the child was REAPED — a confirmed exit — rather than
        swallowing reaping uncertainty: the caller (stop) needs that proof before
        it may claim the server is down. Returns False on a None proc or an
        unconfirmed exit."""
        if proc is None:
            return False
        try:
            proc.terminate()
            proc.wait(timeout=_TERMINATE_WAIT)
            return True
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=_KILL_WAIT)   # reap the killed child; no zombie
                return True
            except Exception:
                # Could not confirm via wait(); report what poll() says so the
                # caller can decide "reaped" vs "still uncertain".
                try:
                    return proc.poll() is not None
                except Exception:
                    return False

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
        self._quarantined = why      # a poisoned server is not safe to reuse
        try:
            self.stop()              # a confirmed stop clears the quarantine
        except Exception:
            pass                     # uncertain stop: the quarantine stays until a clean one

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
        # BOUNDARY GUARD: never start a second process over one we have not
        # confirmed is gone. Refuse while quarantined, while a writer is still
        # in flight, or while the previous proc/reader has not reached a
        # terminal state — starting over any of those would orphan a live child
        # or race a handshake against the old one. A clean, verified stop()
        # clears the quarantine, so a stop RETRY is the recovery path (no
        # auto-restart: the coordinator decides).
        with self._send_lock:
            if self._quarantined is not None:
                raise MCPError(f"cannot start {self.name!r}: {self._quarantined}")
            w = self._writer
            if w is not None and w.is_alive():
                raise MCPError(f"cannot start {self.name!r}: a send is still in flight")
            old_proc = self.proc
            old_reader = self._reader
        if old_proc is not None and old_proc.poll() is None:
            raise MCPError(f"cannot start {self.name!r}: previous process not reaped")
        if (old_reader is not None and old_reader is not threading.current_thread()
                and old_reader.is_alive()):
            raise MCPError(f"cannot start {self.name!r}: previous reader still alive")
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
        """Stop and VERIFY this server. Returns None on a CONFIRMED stop and
        raises MCPError when the cleanup is UNCERTAIN — rather than silently
        claiming success and letting the coordinator drop the handle.

        Verification uses CAPTURED refs, never self.proc / self._reader /
        self._writer read live: a reconnect can replace them mid-teardown.
        Uncertain = the child was not reaped, OR the writer is still alive, OR
        the reader is still alive. Only a clean, verified stop clears the
        quarantine and the writer retention — so a stop() RETRY (once the writer
        has finished) is the recovery path, and nothing auto-restarts.
        """
        with self._send_lock:
            proc = self.proc
            reader = self._reader
            writer = self._writer
            self._quarantined = "stop in progress"
        writer_live = writer is not None and writer.is_alive()

        if proc is not None:
            # Do NOT close stdin while a writer is retained on it: the blocked
            # write holds the BufferedWriter lock, so close() would deadlock.
            # With no live writer the child's read end is the only way to kill it.
            try:
                if proc.stdin and not writer_live:
                    proc.stdin.close()
            except Exception:
                pass
            reaped = self._kill_child(proc)      # terminate -> wait -> kill -> wait
        else:
            reaped = True                        # no child to verify against

        # Join the reader: stdin/stdout closed (or child dead) -> readline() EOF
        # -> _reader_loop exits. Bounded so a wedged reader cannot hang teardown.
        # Self-guard: never join the thread we are running on.
        reader_alive = (reader is not None
                        and reader is not threading.current_thread()
                        and reader.is_alive())
        if reader_alive:
            reader.join(timeout=_READER_JOIN)
            reader_alive = reader.is_alive()

        writer_live = writer is not None and writer.is_alive()
        if not reaped or writer_live or reader_alive:
            # UNCERTAIN: we cannot prove the child is reaped / the writer and
            # reader are terminal. Keep the handle and the quarantine (nothing
            # was cleared) so a caller can verify and RETRY; raising is the
            # honest outcome, not a silent success.
            reasons = ", ".join(
                r for r, bad in (
                    ("child not reaped", not reaped),
                    ("writer still alive", writer_live),
                    ("reader still alive", reader_alive),
                ) if bad
            )
            with self._send_lock:
                self._quarantined = reasons
            raise MCPError(f"stop uncertain for {self.name!r}: {reasons}")

        # Verified clean: clear the process, reader, writer and the quarantine.
        with self._send_lock:
            if self.proc is proc:
                self.proc = None
            if self._reader is reader:
                self._reader = None
            if self._writer is writer:
                self._writer = None
            self._quarantined = None
        return None


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
        if not isinstance(data, dict):
            # Valid JSON, wrong top-level type ([], null, 42): a config error, not
            # an AttributeError on data.get(...). Reported so reconcile preserves.
            errors[p.name] = "not a JSON object"
            continue
        block = data.get("mcpServers") or data.get("servers") or {}
        if not isinstance(block, dict):
            continue
        for name, sc in block.items():
            merged.setdefault(name, sc)
    return merged, errors

#: The file `add` and `remove` write. `.mcp.json` and not `mcp.json`: it is the
#: Claude Code project convention, it is the one this repo already ships, and
#: keeping writes to a single well-known name means a hand-edited `mcp.json`
#: is never rewritten by a tool the user did not point at it.
WRITE_CONFIG_NAME = ".mcp.json"


def _load_doc(path) -> dict:
    """The whole JSON document, or {} when there is nothing usable yet.

    The WHOLE document, because a config file may carry keys this app has never
    heard of — another tool's settings sharing the file. Read-modify-write on
    the parsed doc preserves them; regenerating from `mcpServers` alone would
    silently delete them.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_doc(path, doc: dict) -> None:
    """Write via a temp file in the same directory, then os.replace.

    os.replace is atomic on both platforms, so a reader never observes a
    half-written config — including this app's own reader, which runs on a
    different thread than a dialog's Save. A torn config is not a cosmetic
    problem here: read_server_configs treats an unparseable file as an ERROR
    for every server in it, so one bad write would disconnect everything.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def shadowing_file(root, name: str):
    """The higher-precedence file that already declares `name`, if any.

    read_server_configs gives the EARLIER file the win, so writing a name into
    `.mcp.json` that `mcp.json` already declares produces a write that is real
    on disk and invisible in the app. Refusing with the offending path is the
    only honest outcome: silently succeeding would be the worst of the three.
    """
    for cand in MCP_CONFIG_NAMES:
        if cand == WRITE_CONFIG_NAME:
            break
        p = root / cand
        if p.is_file() and name in (_load_doc(p).get("mcpServers") or {}):
            return p
    return None


def validate_entry(cfg: dict) -> str | None:
    """None if the entry is startable, else why not.

    Mirrors the ONE rule `_build` dispatches on — a `url` without a `command`
    is HTTP, anything else is a stdio spawn — so a config that validates here
    cannot fail to classify there. An entry with neither is the case worth
    catching: it parses as JSON, reads as a server, and can never start.
    """
    if not isinstance(cfg, dict):
        return "entry must be a JSON object"
    if not (cfg.get("command") or cfg.get("url")):
        return "entry needs a `command` (stdio) or a `url` (http)"
    if cfg.get("args") is not None and not isinstance(cfg.get("args"), list):
        return "`args` must be a list"
    if cfg.get("env") is not None and not isinstance(cfg.get("env"), dict):
        return "`env` must be an object"
    return None


class MCPBusy(RuntimeError):
    """A lifecycle op was refused because a maintenance op holds the claim.
    Callers report this as busy and retry manually — never block or cancel the
    holder."""


class MCPManager:
    """Loads mcp.json / .mcp.json (see config_files), starts each server —
stdio or HTTP by entry shape — and exposes specs + a dispatch map."""

    def __init__(self, root: Path):
        self.root = root
        self.servers: dict[str, "MCPServer | HTTPMCPServer"] = {}
        self.failures: dict[str, str] = {}
        #: What the config FILES declare, name -> raw entry. Distinct from
        #: `servers`, which is what is RUNNING. A management surface has to be
        #: able to name a server that exists and is stopped — before this split
        #: the only record of a stopped server was its absence, and absence
        #: cannot be listed, reconnected or removed.
        self.configs: dict[str, dict] = {}
        self._log_handle = None
        #: SHORT lock guarding the claim flag only, so a second caller discovers
        #: "busy" instantly instead of waiting behind a long operation.
        self._claim_lock = threading.Lock()
        #: Non-blocking exclusion claim: exactly one lifecycle op (connect/
        #: disconnect/reconnect/reconcile) at a time. A second caller reports
        #: busy rather than blocking or cancelling the first.
        self._maint_active = False
        #: LONG lock serializing the actual server-dict mutation, held across the
        #: whole op off-loop INCLUDING stop()/join — deadlock-safe because reader
        #: threads never acquire it (they use the per-transport lock). RLock so a
        #: shutdown that must join an in-flight op composes cleanly.
        self._op_lock = threading.RLock()
        #: Permanent terminal state set by stop_all(): once shutting down, no new
        #: connect is allowed, so a connect racing after stop_all cannot start a
        #: server that would then leak (never be stopped).
        self._closing = False
        #: SHORT lock guarding the servers-dict itself, so read-side APIs
        #: (tool_specs/dispatch/describe/status_line) snapshot coherently without
        #: blocking on the long _op_lock while a connect/stop runs. Every dict
        #: mutation and every snapshot takes it, briefly.
        self._servers_lock = threading.Lock()
        #: Names whose stop() failed and whose process is unresolved. A connect
        #: to such a name is refused (never reconnect over a possibly-live
        #: process) until it is cleared by a successful later stop.
        self._stop_failed: set[str] = set()

    # ── plumbing ────────────────────────────────────────────────────────────
    def _log(self):
        """The shared mcp.log handle, opened on first use.

        Lazy because `load()` used to open it only when there was at least one
        server to start; connect() can now be the first thing that runs, and a
        boot with no config must still not create the file.
        """
        if self._log_handle is None:
            self._log_handle = open(
                self.root / MCP_LOG_NAME, "a", encoding="utf-8", errors="replace"
            )
        return self._log_handle

    def _build(self, name: str, sc: dict) -> "MCPServer | HTTPMCPServer":
        """Transport by SHAPE — a `url` without a `command` is HTTP.

        Extracted from load() unchanged so that connect() and load() cannot
        drift into disagreeing about what a config entry means.
        """
        if sc.get("url") and not sc.get("command"):
            return HTTPMCPServer(name, sc, self.root, self._log())
        return MCPServer(name, sc, self.root, self._log())

    def _reload_configs_locked(self) -> dict[str, dict]:
        """Body of reload_configs; assumes the caller holds the claim + op_lock
        (reconcile/add/remove reuse it without re-claiming)."""
        cfg_paths = config_files(self.root)
        servers, file_errors = read_server_configs(cfg_paths)
        self.configs = servers
        self.failures.update(file_errors)
        return servers

    def reload_configs(self) -> dict[str, dict]:
        """Re-read the config files into `configs`. Running servers untouched.

        Deliberately does NOT reconcile: re-reading the file and acting on what
        changed are different decisions, and a reader that also restarted
        things would make `/mcp list` a mutating command. Claim-guarded so a
        config re-read cannot race a reconcile mid-flight."""
        if not self._try_claim():
            raise MCPBusy("MCP maintenance in progress")
        try:
            with self._op_lock:
                return self._reload_configs_locked()
        finally:
            self._release_claim()

    def load(self) -> None:
        """Boot: read the configs and start everything not marked disabled."""
        self.reload_configs()
        for name, sc in self.configs.items():
            if not isinstance(sc, dict) or sc.get("disabled"):
                continue
            self.connect(name)

    # ── lifecycle ───────────────────────────────────────────────────────────
    def _connect_locked(self, name: str) -> str | None:
        """Start ONE server from its config. Returns None, or the error text.

        An error is RETURNED rather than raised because every caller — boot,
        the /mcp command, the dialog — wants to carry on with the others and
        show what went wrong. One bad server must not cost the rest, and must
        not be silent either: a tool that never appears is indistinguishable
        from one the model simply chose not to call.
        """
        if self._closing:
            # A connect racing after stop_all would start a server nothing will
            # ever stop. Refuse rather than leak it.
            return "closing: the MCP manager is shutting down"
        if name in self._stop_failed:
            # An earlier stop() did not confirm; the old process may still be
            # live. Never start a second one over it.
            return f"stop unresolved for {name!r}: restart required before reconnecting"
        sc = self.configs.get(name)
        if not isinstance(sc, dict):
            return f"no server named {name!r} in {' / '.join(MCP_CONFIG_NAMES)}"
        if name in self.servers:
            return None                       # already connected; idempotent
        srv = self._build(name, sc)
        try:
            srv.start()
        except Exception as e:
            runtime_log.record(
                "mcp_server_start_failed",
                site="mcp.manager.connect",
                component="mcp",
                server=name,
                operation="start",
                error_type=type(e).__name__,
            )
            self.failures[name] = f"{type(e).__name__}: {e}"
            try:
                srv.stop()
            except Exception:
                # Cleanup ALSO failed: the child may be live. RETAIN + quarantine
                # the handle rather than orphaning the process, and still return
                # the start failure (never let it escape as an exception).
                with self._servers_lock:
                    self.servers[name] = srv
                self._stop_failed.add(name)
            return self.failures[name]
        with self._servers_lock:
            self.servers[name] = srv
        self.failures.pop(name, None)          # a success clears the old error
        return None

    def _disconnect_locked(self, name: str) -> bool:
        """Stop ONE server and forget it. It stays in `configs`.

        Returns whether anything was running. Runtime-only by design: the file
        still declares the server, so this does not survive a restart. Making
        it persist is `mcp_disabled_servers`' job, and conflating the two would
        mean a transient disconnect quietly rewrote the user's config.
        """
        with self._servers_lock:
            srv = self.servers.pop(name, None)
        if srv is None:
            return False
        try:
            srv.stop()
        except Exception as e:
            runtime_log.record(
                "mcp_server_stop_failed",
                site="mcp.manager.disconnect",
                component="mcp",
                server=name,
                operation="stop",
                error_type=type(e).__name__,
            )
            # RETAIN ownership + quarantine: a failed stop leaves a possibly-live
            # process, so keep the handle and refuse a reconnect until a later
            # stop confirms. Reporting "disconnected" here would be a lie.
            with self._servers_lock:
                self.servers[name] = srv
            self._stop_failed.add(name)
            self.failures[name] = f"stop failed: {type(e).__name__}: {e}"
            return True
        self._stop_failed.discard(name)   # a clean stop resolves any quarantine
        return True

    # ── exclusion claim (short lock) + public claim-guarded wrappers ─────────
    def _try_claim(self) -> bool:
        """Non-blocking: claim the single maintenance slot, or return False fast
        (a second caller must report busy, never block or cancel the holder)."""
        with self._claim_lock:
            if self._maint_active:
                return False
            self._maint_active = True
            return True

    def _release_claim(self) -> None:
        with self._claim_lock:
            self._maint_active = False

    def connect(self, name: str) -> str | None:
        """Public: claim-guarded single connect. Raises MCPBusy if a maintenance
        op holds the claim (report busy, retry manually)."""
        if not self._try_claim():
            raise MCPBusy("MCP maintenance in progress")
        try:
            with self._op_lock:
                return self._connect_locked(name)
        finally:
            self._release_claim()

    def disconnect(self, name: str) -> bool:
        """Public: claim-guarded single disconnect. Raises MCPBusy if busy."""
        if not self._try_claim():
            raise MCPBusy("MCP maintenance in progress")
        try:
            with self._op_lock:
                return self._disconnect_locked(name)
        finally:
            self._release_claim()

    def reconnect(self, name: str) -> str | None:
        """Public: claim-guarded stop-then-start from a FRESH object. Raises
        MCPBusy if busy. A new object, never start() on the stopped one — both
        transports carry per-connection state stop() does not reset. A config
        edit needs reload_configs() first, which /mcp does."""
        if not self._try_claim():
            raise MCPBusy("MCP maintenance in progress")
        try:
            with self._op_lock:
                self._disconnect_locked(name)
                return self._connect_locked(name)
        finally:
            self._release_claim()

    def reconcile(self) -> dict[str, str]:
        """Re-read config and reconcile OWNED connections to match, off-loop and
        claim-serialized. Returns {name: outcome} — or {"": "busy: ..."} when a
        lifecycle op already holds the claim, or {"": "config invalid: ..."} when
        the config cannot be parsed (prior config/servers PRESERVED, no bulk
        disconnect). Outcomes: connected | reconnected | disconnected | failed:...

        Staging: additions and changes are applied BEFORE removals. A changed
        server on an exclusive endpoint is stop-then-start, reported as an outage
        on failure with NO rollback claim (a stopped subprocess is gone). Only
        PREVIOUSLY-DECLARED, owned servers now undeclared are disconnected;
        servers that were never declared (orphans) are left untouched.
        """
        if not self._try_claim():
            return {"": "busy: an MCP maintenance operation is already in progress"}
        try:
            with self._op_lock:
                prior_cfg = dict(self.configs)
                merged, errors = read_server_configs(config_files(self.root))
                if errors:
                    # parse/unreadable: preserve everything, change nothing.
                    self.failures.update(errors)
                    return {"": f"config invalid ({'; '.join(errors.values())}); no changes"}
                # A valid EMPTY config is an intentional remove-all, not an error.
                self.configs = merged
                new_set, prior_set = set(merged), set(prior_cfg)
                outcomes: dict[str, str] = {}

                def _drop(name: str) -> str:
                    """Disconnect and report honestly: a failed stop is quarantined
                    and reported failed (ownership retained), not 'disconnected'."""
                    self._disconnect_locked(name)
                    return (f"failed: {self.failures.get(name, 'stop failed')}"
                            if name in self._stop_failed else "disconnected")

                for name in sorted(new_set):
                    sc = merged[name]
                    if not isinstance(sc, dict):
                        # A MALFORMED entry is NOT an intended removal: never take
                        # down a healthy running server for a typo. Keep the
                        # last-known-good config entry and the running server;
                        # report the new entry failed. (A DISABLED valid entry,
                        # below, IS an intended stop.)
                        prior = prior_cfg.get(name)
                        if isinstance(prior, dict):
                            self.configs[name] = prior
                        outcomes[name] = ("failed: invalid config entry (server/config retained)"
                                          if name in self.servers
                                          else "failed: invalid config entry (not an object)")
                        continue
                    if sc.get("disabled"):
                        # A declared-but-disabled server must match load()'s policy:
                        # not running. Disconnect it if it is.
                        if name in self.servers:
                            outcomes[name] = _drop(name)
                        continue
                    if name not in self.servers:
                        err = self._connect_locked(name)
                        outcomes[name] = "connected" if err is None else f"failed: {err}"
                    elif prior_cfg.get(name) != sc:
                        # exclusive endpoint forces stop-then-start (outage window)
                        self._disconnect_locked(name)
                        if name in self._stop_failed:
                            outcomes[name] = f"failed (stop unresolved): {self.failures.get(name, '')}"
                            continue
                        err = self._connect_locked(name)
                        outcomes[name] = "reconnected" if err is None else f"failed (outage): {err}"
                for name in sorted((prior_set & set(self.servers)) - new_set):
                    outcomes[name] = _drop(name)
                return outcomes
        finally:
            self._release_claim()

    def describe(self) -> list[dict]:
        """One row per server the CONFIGS declare, plus any orphan running one.

        The union matters: a server removed from the file while still running
        must remain visible, or the UI offers no way to stop the thing the user
        can see in their process list.
        """
        with self._servers_lock:
            servers = dict(self.servers)            # coherent snapshot, short lock
        names = list(self.configs) + [n for n in servers if n not in self.configs]
        rows = []
        for name in names:
            raw = self.configs.get(name)
            sc = raw if isinstance(raw, dict) else {}
            invalid_entry = raw is not None and not isinstance(raw, dict)
            srv = servers.get(name)
            declared = name in self.configs
            # ORPHAN OUTRANKS CONNECTED, and that ordering is the point: a
            # running server the file no longer declares IS connected, so the
            # obvious `if srv: "connected"` is true and useless — it hides the
            # one fact the user needs, which is that a restart will not bring
            # this back. Naming the state is the only way the row can say so.
            srv_error = getattr(srv, "error", None) if srv is not None else None
            if invalid_entry:
                state, error = "failed", "invalid config entry (not an object)"
            elif srv is not None and srv_error:
                # A running transport that has POISONED at runtime is not healthy;
                # surface it rather than reporting a bare "connected".
                state, error = "failed", srv_error
            elif srv is not None:
                state = "connected" if declared else "orphan"
                error = self.failures.get(name)
            elif name in self.failures:
                state, error = "failed", self.failures.get(name)
            elif sc.get("disabled"):
                state, error = "disabled", None
            else:
                state, error = "stopped", None
            http = bool(sc.get("url") and not sc.get("command"))
            rows.append(
                {
                    "name": name,
                    "transport": "http" if http else "stdio",
                    "target": sc.get("url") or sc.get("command") or "",
                    "state": state,
                    "tools": len(srv.tools) if srv is not None else 0,
                    "error": error,
                }
            )
        return rows

    def tool_specs(self) -> list[dict]:
        specs: list[dict] = []
        with self._servers_lock:
            servers = list(self.servers.items())    # snapshot; no size-change race
        for sname, srv in servers:
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
        with self._servers_lock:
            servers = list(self.servers.items())    # snapshot; closures bind these refs
        for sname, srv in servers:
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
        with self._servers_lock:
            servers = list(self.servers.items())
        bits = [f"{n}:{len(s.tools)}" for n, s in servers]
        for n, err in self.failures.items():
            bits.append(f"{n}:FAILED")
        return " · ".join(bits)

    # ── config-mutating verbs ───────────────────────────────────────────────
    def add(self, name: str, cfg: dict, *, connect: bool = True) -> str | None:
        """Declare a server in .mcp.json and (by default) start it now.

        Returns None, or the reason it did not happen. Validation runs BEFORE
        the write so a rejected entry never reaches disk — the alternative is a
        file that has to be hand-repaired after a typo in a dialog.
        """
        name = (name or "").strip()
        if not name:
            return "a server needs a name"
        bad = validate_entry(cfg)
        if bad:
            return bad
        if not self._try_claim():
            raise MCPBusy("MCP maintenance in progress")
        try:
            with self._op_lock:
                self._reload_configs_locked()
                # ⚠️ THE SHADOW CHECK RUNS FIRST, and the order is the whole value
                # of the message. `configs` is the MERGE of both files, so a name
                # living in mcp.json also satisfies "already declared" — and that
                # generic answer sends the user to a `remove` that will itself
                # refuse, because /mcp does not write mcp.json. Naming the
                # shadowing file is the only actionable one.
                shadow = shadowing_file(self.root, name)
                if shadow is not None:
                    return (
                        f"{shadow.name} already declares {name!r} and wins on precedence; "
                        f"a write to {WRITE_CONFIG_NAME} would never be read"
                    )
                if name in self.configs:
                    return f"{name!r} is already declared in {WRITE_CONFIG_NAME}"
                path = self.root / WRITE_CONFIG_NAME
                from litetui.shared_state import coordinated_write
                with coordinated_write(path):
                    doc = _load_doc(path)
                    block = doc.setdefault("mcpServers", {})
                    if name in block:
                        return f"{name!r} is already declared in {WRITE_CONFIG_NAME}"
                    block[name] = cfg
                    _save_doc(path, doc)
                self._reload_configs_locked()
                return self._connect_locked(name) if connect else None
        finally:
            self._release_claim()

    def remove(self, name: str) -> str | None:
        """Stop it and delete its entry from .mcp.json. Returns None, or why not.

        Stops FIRST: deleting the declaration of a running server would leave a
        process with no config behind it, which describe() can only report as
        an orphan. Doing it in this order means `remove` has no such aftermath.
        """
        if not self._try_claim():
            raise MCPBusy("MCP maintenance in progress")
        try:
            with self._op_lock:
                path = self.root / WRITE_CONFIG_NAME
                from litetui.shared_state import coordinated_write
                with coordinated_write(path):
                    doc = _load_doc(path)
                    block = doc.get("mcpServers") or {}
                    if name not in block:
                        other = shadowing_file(self.root, name)
                        if other is not None:
                            return f"{name!r} is declared in {other.name}, which /mcp does not write"
                        return f"no server named {name!r} in {WRITE_CONFIG_NAME}"
                    # Only stop the running server if THIS (.mcp.json) entry is
                    # the effective one. If a higher-precedence mcp.json also
                    # declares it, that server stays effective and running —
                    # stopping it here would leave it declared-but-stopped.
                    if shadowing_file(self.root, name) is None:
                        self._disconnect_locked(name)
                    del block[name]
                    doc["mcpServers"] = block
                    _save_doc(path, doc)
                self._reload_configs_locked()
                self.failures.pop(name, None)
                return None
        finally:
            self._release_claim()

    def stop_all(self) -> None:
        """Shutdown: JOIN any in-flight lifecycle op (bounded by that op's own
        connect/stop timeouts) via _op_lock, then stop every server. Never
        refuses busy and never leaks a transport — shutdown is terminal.

        A server whose stop() comes back UNCERTAIN (raises) is RETAINED: kept in
        `servers` and flagged in `_stop_failed` with a truthful failure, rather
        than silently discarded. Only cleanly-stopped servers are removed —
        dropping an uncertain handle here would orphan a possibly-live child."""
        # Set BEFORE acquiring the lock so a connect racing right now is refused
        # immediately (see _connect_locked) rather than starting a server this
        # shutdown will not see.
        self._closing = True
        with self._op_lock:
            with self._servers_lock:
                servers = list(self.servers.items())   # name -> server snapshot
            # do NOT clear up front: retention below decides what stays.
        for name, s in servers:
            try:
                s.stop()
            except Exception as e:
                # Uncertain cleanup: the child may be live. RETAIN the handle +
                # quarantine so a later stop can confirm; a lie ("stopped") would
                # be worse than keeping the reference.
                runtime_log.record(
                    "mcp_server_stop_failed",
                    site="mcp.manager.stop_all",
                    component="mcp",
                    server=name,
                    operation="stop",
                    error_type=type(e).__name__,
                )
                with self._servers_lock:
                    self.servers[name] = s
                self._stop_failed.add(name)
                self.failures[name] = f"stop failed: {type(e).__name__}: {e}"
                continue
            with self._servers_lock:
                self.servers.pop(name, None)
            self._stop_failed.discard(name)
            self.failures.pop(name, None)
        if self._log_handle:
            try:
                self._log_handle.close()
            except Exception:
                pass
