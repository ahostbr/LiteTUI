"""Reviewed current-agent memory edits exercise the real owned writer and independent readback."""
from __future__ import annotations

import hashlib
import os

import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_armed
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import gui_rpc, native_context
from litetui.agent_store import StoreError

app, armed, switch_app = owned_app, owned_armed, owned_switch_app
pytestmark = pytest.mark.asyncio


def request(app, action, doc="memory.md", **fields):
    return {"id": action + "-id", "version": 1, **work.identity(app), "work_instance": work.bridge(app).instance,
            "type": "gui.context.native_" + action, "document": doc, **fields}


@pytest.mark.parametrize("doc", ["memory.md", "soul.md", "handoff.md"])
async def test_current_memory_writer_preserves_exact_draft_and_verifies_on_disk(armed, doc):
    path = armed._agent_session.memory_root / doc
    old = "Before\r\nUTF-8 🍃\n".encode()
    after = b"After\r\nline two\n"
    path.write_bytes(old)
    before = hashlib.sha256(old).hexdigest()
    opened = await gui_rpc.async_dispatch(armed, request(armed, "edit", doc, revision=before))
    assert opened["text"].encode() == old
    r = await gui_rpc.async_dispatch(armed, request(armed, "review", doc, revision=before, text=after.decode()))
    assert path.read_bytes() == old
    assert (r["before_bytes"], r["after_bytes"]) == (len(old), len(after))
    apply = request(armed, "apply", doc, token=r["token"])
    a = await gui_rpc.async_dispatch(armed, apply)
    assert path.read_bytes() == after and a["revision"] == hashlib.sha256(after).hexdigest()
    assert await gui_rpc.async_dispatch(armed, apply) == a
    with pytest.raises(ValueError, match="already applied"):
        await gui_rpc.async_dispatch(armed, {**apply, "id": "different"})
    read = request(armed, "readback", doc, token=r["token"], apply_id=apply["id"])
    assert (await gui_rpc.async_dispatch(armed, read))["kind"] == "context_saved"
    path.write_bytes(b"external changes")
    with pytest.raises(ValueError, match="after apply"):
        await gui_rpc.async_dispatch(armed, read)
    assert path.read_bytes() == b"external changes"


async def test_missing_empty_and_guarded_publication(armed, monkeypatch):
    path = armed._agent_session.memory_root / "memory.md"
    path.unlink(missing_ok=True)
    review = request(armed, "review", revision=None, text="")
    r = gui_rpc.dispatch(armed, review)
    assert not path.exists() and r["before_bytes"] is None and r["after_bytes"] == 0
    publish = request(armed, "apply", token=r["token"])
    real_link = os.link
    def raced(source, target):
        target.write_bytes(b"unrelated author")
        return real_link(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(os, "link", raced)
        with pytest.raises(FileExistsError):
            gui_rpc.dispatch(armed, publish)
    assert path.read_bytes() == b"unrelated author"
    path.unlink()
    r = gui_rpc.dispatch(armed, review)
    gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert path.read_bytes() == b""
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(armed, request(armed, "edit", revision=None))
    empty = hashlib.sha256(b"").hexdigest()
    assert gui_rpc.dispatch(armed, request(armed, "edit", revision=empty))["revision"] == empty


async def test_stale_review_or_prepublication_change_preserves_current_file(armed, monkeypatch):
    path = armed._agent_session.memory_root / "soul.md"
    path.write_bytes(b"original")
    revision = hashlib.sha256(path.read_bytes()).hexdigest()
    cmd = request(armed, "review", "soul.md", revision=revision, text="new")
    r = gui_rpc.dispatch(armed, cmd)
    path.write_bytes(b"concurrent changes")
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(armed, request(armed, "apply", "soul.md", token=r["token"]))
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(armed, cmd)
    assert path.read_bytes() == b"concurrent changes"
    path.write_bytes(b"original")
    r = gui_rpc.dispatch(armed, cmd)
    regular = native_context._regular_file
    def race(temporary):
        if temporary.suffix == ".tmp":
            path.write_bytes(b"just before publish")
        return regular(temporary)
    monkeypatch.setattr(native_context, "_regular_file", race)
    with pytest.raises(ValueError, match="before publication"):
        gui_rpc.dispatch(armed, request(armed, "apply", "soul.md", token=r["token"]))
    assert path.read_bytes() == b"just before publish"


async def test_refuse_wrong_owner_wrong_token_extra_fields_and_linked_files(armed, tmp_path, monkeypatch):
    path = armed._agent_session.memory_root / "memory.md"
    path.unlink(missing_ok=True)
    for doc in ("system", "hooks", "memories/note.md", "../soul.md"):
        with pytest.raises(ValueError, match="Only the current agent"):
            gui_rpc.dispatch(armed, request(armed, "review", doc, revision=None, text="no"))
    cmd = request(armed, "review", revision=None, text="safe")
    for extra in ({"path": str(tmp_path)}, {"work_instance": "x"}, {"agent_id": "x"},
                  {"text": "🌱" * 65537}, {"revision": "not-a-revision"}):
        with pytest.raises(ValueError):
            gui_rpc.dispatch(armed, {**cmd, **extra})
    r = gui_rpc.dispatch(armed, cmd)
    for bad in (request(armed, "apply", "soul.md", token=r["token"]),
                request(armed, "apply", token="x"), request(armed, "readback", token=r["token"], apply_id="x")):
        with pytest.raises(ValueError):
            gui_rpc.dispatch(armed, bad)
    with monkeypatch.context() as patch:
        patch.setattr(armed, "_owner_memory_dir", tmp_path)
        with pytest.raises(ValueError, match="Owner memory"):
            gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    outside = tmp_path / "external.txt"
    outside.write_bytes(b"external")
    os.link(outside, path)
    with pytest.raises((StoreError, ValueError), match="regular unlinked"):
        gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert outside.read_bytes() == path.read_bytes() == b"external"
