"""Owner-local attachment to the existing GUI JSONL operations (not stdout RPC).

The server runs on Textual's asyncio loop, so all commands use the existing
management owner and dispatcher. The token grants control to this OS user; it
is not protection from another process running as that same user.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

MAX_FRAME = 1024 * 1024
IDLE_TIMEOUT = 30
OPERATIONS = frozenset({
    "state", "models.load", "models.unload", "models.select", "models.reconnect",
    "thinking.set", "context.set", "backend.set", "engine.start", "engine.stop",
    "engine.status", "conversations.compact", "conversations.read", "prompt.submit",
    "models.capabilities",
})


def worktree_port(path):
    """Same UTF-16 / signed 32-bit mapping as shared/worktreePort.ts."""
    value = 0
    raw = str(path).encode("utf-16-le")
    for i in range(0, len(raw), 2):
        value = (31 * value + int.from_bytes(raw[i:i + 2], "little")) & 0xffffffff
    if value >= 0x80000000:
        value -= 0x100000000
    return 4100 + abs(value) % 100


def _private_file(path, document):
    """Create the secret with restricted permissions BEFORE writing any bytes."""
    if os.name != "nt":
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
            os.close(fd)
            raise PermissionError("Cannot protect local RPC token")
    else:
        fd = _windows_private_file(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(document, output)
    except BaseException:
        # This call created the file exclusively; a failed write must not leave
        # a partial credential behind. Never remove an existing caller's file.
        path.unlink(missing_ok=True)
        raise


def _windows_private_file(path):
    """A protected DACL for the process user; chmod is NOT an ACL on Windows."""
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt

    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    adv.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)]
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE

    class SecurityAttributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]

    token, sid_text, descriptor = w.HANDLE(), w.LPWSTR(), c.c_void_p()
    try:
        if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(token)):
            raise c.WinError(c.get_last_error())
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, c.byref(size))
        buffer = c.create_string_buffer(size.value)
        if not adv.GetTokenInformation(token, 1, buffer, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        if not adv.ConvertSidToStringSidW(sid, c.byref(sid_text)):
            raise c.WinError(c.get_last_error())
        # Protected (P), no inherited ACEs, only the current user's SID.
        sddl = f"O:{sid_text.value}D:P(A;;FA;;;{sid_text.value})"
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None):
            raise c.WinError(c.get_last_error())
        attributes = SecurityAttributes(c.sizeof(SecurityAttributes), descriptor, False)
        handle = kernel.CreateFileW(str(path), 0x40000000, 0, c.byref(attributes), 1, 0x80, None)
        if handle == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            return msvcrt.open_osfhandle(handle, os.O_WRONLY)
        except Exception:
            kernel.CloseHandle(handle)
            raise
    finally:
        if token:
            kernel.CloseHandle(token)
        if sid_text:
            kernel.LocalFree(c.cast(sid_text, c.c_void_p))
        if descriptor:
            kernel.LocalFree(descriptor)


def _presence(endpoint, *, remove=False):
    """Never create or take over a registry row; only this live owner's metadata."""
    from litetui import harness
    if harness.harness_disabled():
        return False
    path = Path.home() / ".liteharness" / "agents" / f"{endpoint['agent_id']}.json"
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(row, dict) or row.get("agent_id") != endpoint["agent_id"]
                or row.get("session_pid") != endpoint["pid"]):
            return False
        original = dict(row)
        if remove:
            if row.get("litetui_rpc") != endpoint:
                return False
            row.pop("litetui_rpc", None)
        else:
            row["litetui_rpc"] = endpoint
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(json.dumps(row), encoding="utf-8")
            # Recheck owner before replace; no successor resurrection.
            latest = json.loads(path.read_text(encoding="utf-8"))
            # A concurrent heartbeat may retain our PID but update other
            # fields. Do not replace that newer document with a stale copy.
            if latest != original:
                return False
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


class LocalRpc:
    def __init__(self, app):
        self.app = app
        self.server = None
        self.clients = set()
        self.requests = set()
        self.closing = False
        self.secret = secrets.token_hex(32)
        self.endpoint = None
        self.token_path = None
        self._owns_token = False

    async def start(self):
        from litetui import harness, paths
        if harness.harness_disabled() or not self.app.seat.registered:
            return False
        root = paths.data_root() / "rpc" / self.app.seat.agent_id
        root.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_hex(16)
        self.token_path = root / f"{nonce}.json"
        identity = {"agent_id": self.app.seat.agent_id, "pid": os.getpid(), "nonce": nonce}
        try:
            _private_file(self.token_path, {**identity, "token": self.secret})
            self._owns_token = True
            start = worktree_port(Path.cwd())
            for port in range(start, start + 100):
                try:
                    self.server = await asyncio.start_server(self._client, "127.0.0.1", port, limit=MAX_FRAME + 1)
                    break
                except OSError:
                    continue
            if self.server is None:
                raise OSError("No local RPC port available")
            self.endpoint = {**identity, "host": "127.0.0.1", "port": port,
                             "token_path": str(self.token_path.resolve())}
            if not _presence(self.endpoint):
                raise PermissionError("Local RPC presence owner changed")
            return True
        except BaseException:
            # Textual cancels workers during shutdown; cancellation also owns
            # closing a partially started server and its private credential.
            await self.close()
            raise

    async def _frame(self, reader):
        raw = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
        if not raw or len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
            raise ValueError("Missing or oversized JSONL frame")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("JSONL frame must be an object")
        return result

    async def _reply(self, writer, result):
        raw = (json.dumps(result, default=str) + "\n").encode("utf-8")
        if len(raw) > MAX_FRAME:
            raw = (json.dumps({"type": "response", "id": result.get("id"), "ok": False,
                               "error": "Response exceeds local RPC frame limit"}) + "\n").encode("utf-8")
        writer.write(raw)
        await asyncio.wait_for(writer.drain(), IDLE_TIMEOUT)

    async def _client(self, reader, writer):
        self.clients.add(writer)
        task = asyncio.current_task()
        self.requests.add(task)
        authenticated = False
        cmd_id = None
        try:
            peer = writer.get_extra_info("peername")
            if not peer or peer[0] != "127.0.0.1" or self.closing or len(self.clients) > 16:
                return
            auth = await self._frame(reader)
            if (auth.get("agent_id") != self.endpoint["agent_id"] or auth.get("nonce") != self.endpoint["nonce"]
                    or not isinstance(auth.get("token"), str)
                    or not hmac.compare_digest(auth["token"].encode("utf-8"), self.secret.encode("utf-8"))):
                await self._reply(writer, {"type": "response", "ok": False, "error": "Local RPC authentication refused"})
                return
            await self._reply(writer, {"type": "authenticated", "agent_id": self.endpoint["agent_id"], "nonce": self.endpoint["nonce"]})
            authenticated = True
            # One request per connection: no duplicate/replay/retry ambiguity.
            cmd = await self._frame(reader)
            cmd_id = cmd.get("id")
            if not isinstance(cmd_id, str) or not 1 <= len(cmd_id) <= 128:
                raise ValueError("Command requires a bounded string id")
            operation = cmd.get("type")
            if not isinstance(operation, str) or not operation.startswith("gui.") or operation[4:] not in OPERATIONS:
                raise ValueError("Operation is not available through local attachment")
            from litetui.gui_rpc import async_dispatch
            try:
                # start_server and this coroutine run on the app's existing UI loop.
                result = await async_dispatch(self.app, cmd)
                response = {"type": "response", "id": cmd_id, "ok": True, "result": result}
            except Exception as exc:
                response = {"type": "response", "id": cmd_id, "ok": False, "error": str(exc)}
            await self._reply(writer, response)
        except (ValueError, OSError, TimeoutError, asyncio.LimitOverrunError):
            # Never echo untrusted frames or bearer tokens in an error.
            if authenticated:
                try:
                    await self._reply(writer, {'type': 'response', 'id': cmd_id, 'ok': False,
                                               'error': 'Invalid, timed out or unavailable local RPC operation'})
                except (OSError, TimeoutError):
                    pass
        finally:
            self.clients.discard(writer)
            self.requests.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def close(self):
        self.closing = True
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for writer in tuple(self.clients):
            writer.close()
        pending = tuple(self.requests)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.endpoint:
            _presence(self.endpoint, remove=True)
        if self.token_path and self._owns_token:
            self.token_path.unlink(missing_ok=True)
            self._owns_token = False
        self.secret = ""
