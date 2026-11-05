"""Nested topics use the leased agent writer and the independently checked Context readback."""

from __future__ import annotations

import hashlib
import os

import pytest
from test_rust_context_edit import app as owned_app
from test_rust_context_edit import armed as owned_armed
from test_rust_context_edit import request
from test_rust_context_edit import switch_app as owned_switch_app

from litetui import gui_rpc, native_context
from litetui import native_context_topics as topics

app, armed, switch_app = owned_app, owned_armed, owned_switch_app
pytestmark = pytest.mark.asyncio


def page(app, name="topics", **fields):
    return gui_rpc.dispatch(
        app,
        request(app, "topic_page", name, **{"offset": 0, "revision": None, **fields}),
    )


def edit(app, action, document, **fields):
    return gui_rpc.dispatch(app, request(app, "topic_" + action, document, **fields))


async def test_current_catalog_and_nested_read_are_bounded_lossless_and_revision_checked(
    armed, monkeypatch
):
    root = armed._agent_session.memory_root
    folder = root / "memories"
    assert page(armed)["status"] == "empty"
    nested = folder / "project"
    nested.mkdir(parents=True, exist_ok=True)
    names = [f"memories/project/{i:03}-" + "雪" * 26 + ".md" for i in range(65)]
    for name in names:
        (root / name).write_bytes(b"")
    payload = ("x" * 4094 + "🌿\r\nlast").encode()
    (root / names[3]).write_bytes(payload)
    (nested / "ignored.bin").write_bytes(b"never returned")
    first = page(armed)
    expected = "\n".join(names).encode()
    assert (
        first["total_bytes"] == len(expected)
        and first["revision"] == hashlib.sha256(expected).hexdigest()
    )
    second = page(armed, revision=first["revision"], offset=first["next_offset"])
    assert (first["text"] + second["text"]).encode() == expected and second[
        "next_offset"
    ] is None
    content = page(armed, names[3])
    tail = page(
        armed, names[3], revision=content["revision"], offset=content["next_offset"]
    )
    assert (content["text"] + tail["text"]).encode() == payload
    (root / names[0]).unlink()
    assert (
        page(armed, revision=first["revision"], offset=first["next_offset"])["status"]
        == "changed"
    )
    monkeypatch.setattr(topics, "MAX_ENTRIES", 3)
    assert page(armed)["status"] == "oversize"


async def test_reviewed_create_empty_conflict_edit_and_exact_readback(armed):
    root = armed._agent_session.memory_root
    name = "memories/project/nested/notes.md"
    path = root / name
    assert page(armed, name)["status"] == "missing"
    draft = "Prefers UTF-8 🍃\r\n"
    review = edit(armed, "review", name, revision=None, text=draft)
    assert not path.parent.exists() and review["before_bytes"] is None
    path.parent.mkdir(parents=True)
    path.write_bytes(b"other writer")
    with pytest.raises(ValueError, match="changed"):
        edit(armed, "apply", name, token=review["token"])
    assert path.read_bytes() == b"other writer"
    path.unlink()
    review = edit(armed, "review", name, revision=None, text="")
    applied = edit(armed, "apply", name, token=review["token"])
    saved = edit(
        armed, "readback", name, token=review["token"], apply_id=applied["apply_id"]
    )
    assert (
        page(armed, name)["status"] == "empty"
        and saved["revision"] == hashlib.sha256(b"").hexdigest()
    )
    rev = saved["revision"]
    assert edit(armed, "edit", name, revision=rev)["text"] == ""
    review = edit(armed, "review", name, revision=rev, text=draft)
    with pytest.raises(ValueError):
        edit(armed, "apply", "memories/other.md", token=review["token"])
    applied = edit(armed, "apply", name, token=review["token"])
    assert path.read_bytes() == draft.encode()
    assert edit(
        armed, "readback", name, token=review["token"], apply_id=applied["apply_id"]
    )["bytes"] == len(draft.encode())
    path.write_bytes(b"changed after apply")
    with pytest.raises(ValueError, match="after apply"):
        edit(
            armed, "readback", name, token=review["token"], apply_id=applied["apply_id"]
        )


async def test_scope_aliases_links_identity_and_old_operations_fail_closed(
    armed, tmp_path, monkeypatch
):
    root = armed._agent_session.memory_root
    folder = root / "memories" / "Project"
    folder.mkdir(parents=True, exist_ok=True)
    for name in (
        "memory.md",
        "memories/../outside.md",
        "memories/CON.md",
        "memories/Project\\bad.md",
        "memories/project/alias.md",
        "memories/Project/NOTES.MD",
        "memories/" + "q/" * 17 + "x.md",
    ):
        with pytest.raises(ValueError):
            page(armed, name)
        with pytest.raises(ValueError):
            edit(armed, "review", name, revision=None, text="bad")
    name = "memories/Project/notes.md"
    with pytest.raises(ValueError):
        gui_rpc.dispatch(armed, request(armed, "edit", name, revision=None))
    for bad in (
        {"work_instance": "foreign"},
        {"agent_id": "foreign"},
        {"session_id": "foreign"},
    ):
        with pytest.raises(ValueError):
            page(armed, name, **bad)
    with monkeypatch.context() as patch:
        patch.setattr(armed, "_owner_memory_dir", tmp_path)
        with pytest.raises(ValueError):
            page(armed)
    outside = tmp_path / "external.md"
    outside.write_bytes(b"outside canary")
    reviewed = edit(armed, "review", name, revision=None, text="unsafe if applied")
    os.link(outside, root / name)
    for fn in (
        lambda: page(armed),
        lambda: page(armed, name),
        lambda: edit(armed, "apply", name, token=reviewed["token"]),
    ):
        with pytest.raises(ValueError):
            fn()
    assert outside.read_bytes() == (root / name).read_bytes() == b"outside canary"
    (root / name).unlink()
    (root / name).write_bytes(b"z" * (native_context.MAX_DOCUMENT + 1))
    assert page(armed, name)["status"] == "oversize"
    with pytest.raises(ValueError, match="editing limit"):
        edit(armed, "edit", name, revision=None)


async def test_linked_topic_directory_is_never_traversed_or_written(armed, tmp_path):
    folder = armed._agent_session.memory_root / "memories"
    folder.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    private = outside / "private.md"
    private.write_bytes(b"private")
    try:
        (folder / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Test filesystem cannot create a symbolic link: {exc}")
    name = "memories/linked/private.md"
    for fn in (
        lambda: page(armed),
        lambda: page(armed, name),
        lambda: edit(armed, "review", name, revision=None, text="overwritten"),
    ):
        with pytest.raises(ValueError, match="Linked"):
            fn()
    assert private.read_bytes() == b"private"
