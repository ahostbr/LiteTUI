"""Isolated ownership metadata cleanup, timeout and private Windows ACL evidence."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from litetui import harness, local_rpc


def test_presence_identity_and_conditional_cleanup(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".liteharness" / "agents" / "fake.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"agent_id": "fake", "session_pid": 123, "name": "Preserved"}))
    endpoint = {"agent_id": "fake", "pid": 123, "nonce": "old", "token_path": "fakepath"}
    assert local_rpc._presence(endpoint)
    row = json.loads(path.read_text())
    assert row["name"] == "Preserved" and row["litetui_rpc"] == endpoint
    successor = {**endpoint, "nonce": "new"}
    row["litetui_rpc"] = successor
    path.write_text(json.dumps(row))
    assert not local_rpc._presence(endpoint, remove=True)
    assert json.loads(path.read_text())["litetui_rpc"] == successor
    assert local_rpc._presence(successor, remove=True)
    assert "litetui_rpc" not in json.loads(path.read_text())
    path.unlink()
    assert not local_rpc._presence(endpoint) and not path.exists()


@pytest.mark.asyncio
async def test_backend_environment_authority_remains_locked(monkeypatch):
    from litetui import gui_rpc
    from litetui.settings import Settings
    monkeypatch.setenv("LITETUI_BACKEND", "codex")
    app = SimpleNamespace(settings=Settings(), _chat_running=lambda: False)
    with pytest.raises(ValueError, match="controlled by LITETUI_BACKEND"):
        await gui_rpc.async_dispatch(app, {"type": "gui.backend.set", "name": "claude"})
    assert not app._gui_management_busy


@pytest.mark.asyncio
async def test_frame_timeout_is_bounded(monkeypatch):
    monkeypatch.setattr(local_rpc, "IDLE_TIMEOUT", .01)
    rpc = local_rpc.LocalRpc(SimpleNamespace())
    with pytest.raises(TimeoutError):
        await rpc._frame(asyncio.StreamReader())


@pytest.mark.skipif(os.name != "nt", reason="Windows native ACL proof")
def test_windows_token_protected_owner_only_acl(tmp_path):
    import ctypes as c
    from ctypes import wintypes as w
    path = tmp_path / "token.json"
    local_rpc._private_file(path, {"token": "fake"})
    adv = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    adv.GetNamedSecurityInfoW.argtypes = [w.LPWSTR, c.c_int, w.DWORD, c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.c_void_p]
    kernel.LocalFree.argtypes = [c.c_void_p]
    descriptor, text = c.c_void_p(), w.LPWSTR()
    try:
        assert adv.GetNamedSecurityInfoW(str(path), 1, 5, None, None, None, None, c.byref(descriptor)) == 0
        assert adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 5, c.byref(text), None)
        sddl = text.value
        assert "D:P" in sddl and sddl.count("(A;") == 1
        owner = sddl.split("O:", 1)[1].split("D:", 1)[0]
        assert f";;;{owner})" in sddl
        assert "WD)" not in sddl and "BU)" not in sddl
    finally:
        if text: kernel.LocalFree(c.cast(text, c.c_void_p))
        if descriptor: kernel.LocalFree(descriptor)


@pytest.mark.parametrize("document", [[], None, "invalid", 42])
def test_malformed_presence_refuses_without_overwriting(monkeypatch, tmp_path, document):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".liteharness" / "agents" / "fake.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    assert not local_rpc._presence({"agent_id": "fake", "pid": 123})
    assert path.read_bytes() == before


def test_presence_recheck_preserves_same_pid_external_edit(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / ".liteharness" / "agents" / "fake.json"
    path.parent.mkdir(parents=True)
    row = {"agent_id": "fake", "session_pid": 123, "model": "old"}
    path.write_text(json.dumps(row))
    write = Path.write_text

    def concurrent_write(self, text, *args, **kwargs):
        result = write(self, text, *args, **kwargs)
        if self.suffix == ".tmp":
            write(path, json.dumps({**row, "model": "new"}))
        return result

    monkeypatch.setattr(Path, "write_text", concurrent_write)
    assert not local_rpc._presence({"agent_id": "fake", "pid": 123})
    assert json.loads(path.read_text())["model"] == "new"


@pytest.mark.asyncio
async def test_failed_exclusive_token_creation_keeps_existing_file(monkeypatch, tmp_path):
    from litetui import paths
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    monkeypatch.setattr(local_rpc.secrets, "token_hex", lambda size: "collision")
    app = SimpleNamespace(seat=SimpleNamespace(registered=True, agent_id="fake"))
    path = tmp_path / "rpc" / "fake" / "collision.json"
    path.parent.mkdir(parents=True)
    path.write_text("existing credential")
    rpc = local_rpc.LocalRpc(app)
    with pytest.raises(OSError):
        await rpc.start()
    assert path.read_text() == "existing credential"


@pytest.mark.asyncio
async def test_cancelled_start_removes_only_owned_token(monkeypatch, tmp_path):
    from litetui import paths
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)

    async def cancelled_server(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "start_server", cancelled_server)
    app = SimpleNamespace(seat=SimpleNamespace(registered=True, agent_id="fake"))
    rpc = local_rpc.LocalRpc(app)
    with pytest.raises(asyncio.CancelledError):
        await rpc.start()
    assert not rpc.token_path.exists()
    assert rpc.closing and rpc.server is None
