"""Exact skill preparation against owned source, with external effects barred."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_rust_backend_catalog import catalog_app as _catalog_app
from test_rust_backend_catalog import tree

from litetui import command_surface, gui_rpc, hook_host, native_skills, skills

skill_app = _catalog_app


def setup(app, tmp_path, data=b"---\nname: Actual\n---\nKeep ${CLAUDE_SKILL_DIR}\r\nlast  \r\n"):
    path = tmp_path / "folder-alias" / "SKILL.md"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(data)
    app.skills = [skills.Skill("actual", "Fixture", path, "fixture")]
    result = command_surface.catalog(app, {"session_id": app.convo_id})
    row = next(row for row in result["entries"] if row["kind"] == "skill")
    request = {key: result[key] for key in ("version", "session_id", "agent_id", "backend", "model", "snapshot")}
    return path, {**request, "row_id": row["id"], "arguments": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["", "\r\n", "  exact \t args\r\nlast  \r\n", "emoji \U0001f680"])
async def test_prepare_preserves_full_frontmatter_binding_and_exact_arguments(skill_app, tmp_path, arguments):
    app = skill_app
    path, request = setup(app, tmp_path)
    request["arguments"] = arguments
    before = tree(app)
    result = await gui_rpc.async_dispatch(app, {"type": "gui.skills.native_prepare", **request})
    expected = "# Skill: actual\n\n" + skills.bind_skill_dir(path.read_bytes().decode(), path.parent)
    assert result["body"] == expected
    assert result["prompt"] == expected + ("\n\n---\n\n" + arguments if arguments else "")
    assert result["arguments"] == arguments
    assert result["prompt_sha256"] == hashlib.sha256(result["prompt"].encode()).hexdigest()
    assert result["file"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["file"]["resolved_path"] == str(path.resolve())
    assert result["skill"] == {"name": "actual", "source": "fixture", "path": str(path)}
    assert native_skills.prepare(app, request) == result
    assert tree(app) == before and gui_rpc.current_operation(app) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changes, message", [
    ({"version": True}, "version"), ({"version": 2}, "version"),
    ({"snapshot": "stale"}, "stale"), ({"agent_id": "foreign"}, "agent"),
    ({"backend": "custom"}, "backend"), ({"model": "other"}, "model"),
    ({"row_id": "missing"}, "exact"), ({"arguments": None}, "text"),
    ({"arguments": "x" * 65537}, "budget"), ({"arguments": "\ud800"}, "UTF-8"),
])
async def test_prepare_rejects_invalid_selection_without_writes(skill_app, tmp_path, changes, message):
    app = skill_app
    _path, request = setup(app, tmp_path)
    before = tree(app)
    with pytest.raises((ValueError, TypeError), match=message):
        native_skills.prepare(app, {**request, **changes})
    assert tree(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault, message", [
    ("missing", "could not"), ("directory", "regular"), ("utf8", "UTF-8"),
    ("source_size", "65536"), ("prompt_size", "budget"), ("binding_size", "budget"),
])
async def test_source_and_expanded_prompt_are_bounded_without_truncation(skill_app, tmp_path, fault, message):
    app = skill_app
    path, request = setup(app, tmp_path)
    if fault == "missing":
        path.unlink()
    elif fault == "directory":
        path.unlink()
        path.mkdir()
    elif fault == "utf8":
        path.write_bytes(b"invalid\xff")
    elif fault == "source_size":
        path.write_bytes(b"x" * 65537)
    elif fault == "prompt_size":
        path.write_bytes(b"x" * 65536)
    else:
        path.write_bytes(b"${CLAUDE_SKILL_DIR}" * 3000)
    before = tree(app)
    with pytest.raises((ValueError, TypeError), match=message):
        native_skills.prepare(app, request)
    assert tree(app) == before


@pytest.mark.asyncio
async def test_exact_budget_and_new_fresh_revision_are_explicit(skill_app, tmp_path):
    app = skill_app
    path, request = setup(app, tmp_path, b"")
    wrapper = native_skills.prepare(app, request)["prompt"]
    path.write_bytes(b"x" * (65536 - len(wrapper.encode())))
    result = native_skills.prepare(app, request)
    assert len(result["prompt"].encode()) == 65536
    path.write_bytes(b"later")
    fresh = native_skills.prepare(app, request)
    assert fresh["file"]["sha256"] != result["file"]["sha256"]
    assert fresh["snapshot"] == result["snapshot"]  # Catalog is not a file version.


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["same_size", "replacement", "object", "policy"])
async def test_mid_preparation_file_or_context_change_cannot_publish(skill_app, tmp_path, monkeypatch, fault):
    app = skill_app
    path, request = setup(app, tmp_path, b"old")
    original = native_skills._read
    calls = 0
    def mutate(selected):
        nonlocal calls
        result = original(selected)
        calls += 1
        if calls == 1:
            if fault == "same_size":
                path.write_bytes(b"new")
                os.utime(path, ns=(result[1].st_atime_ns, result[1].st_mtime_ns))
            elif fault == "replacement":
                swap = path.with_suffix(".replacement")
                swap.write_bytes(b"old")
                swap.replace(path)
            elif fault == "object":
                app.skills[0] = skills.Skill("actual", "Fixture", path, "fixture")
            else:
                old = gui_rpc._tool_policy(app)
                monkeypatch.setattr(gui_rpc, "_tool_policy", lambda _app: {**old, "revision": "changed"})
        return result
    monkeypatch.setattr(native_skills, "_read", mutate)
    with pytest.raises(ValueError, match="changed|stale"):
        native_skills.prepare(app, request)


@pytest.mark.asyncio
async def test_selected_duplicate_name_does_not_rematch_folder_alias(skill_app, tmp_path):
    app = skill_app
    first, request = setup(app, tmp_path, b"first")
    second = tmp_path / "actual" / "SKILL.md"
    second.parent.mkdir()
    second.write_bytes(b"second")
    app.skills.append(skills.Skill("ACTUAL", "Other", second, "other"))
    catalog = command_surface.catalog(app, request)
    request["snapshot"] = catalog["snapshot"]
    for row in catalog["entries"]:
        if row["kind"] == "skill":
            result = native_skills.prepare(app, {**request, "row_id": row["id"]})
            path = first if row["title"] == "actual" else second
            assert result["file"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_stable_file_link_is_a_legitimate_exact_selection(skill_app, tmp_path):
    app = skill_app
    path, _request = setup(app, tmp_path, b"linked")
    linked = tmp_path / "linked.md"
    os.link(path, linked)
    app.skills = [skills.Skill("linked", "Hard linked file", linked, "fixture")]
    catalog = command_surface.catalog(app, {"session_id": app.convo_id})
    row = next(row for row in catalog["entries"] if row["kind"] == "skill")
    request = {key: catalog[key] for key in ("version", "session_id", "agent_id", "backend", "model", "snapshot")}
    result = native_skills.prepare(app, {**request, "row_id": row["id"], "arguments": ""})
    assert result["skill"]["path"] == str(linked) and result["prompt"].endswith("linked")

@pytest.mark.asyncio
@pytest.mark.parametrize("source, value, expected", [
    ("rpc", "  exact\r\nlast  \r\n", "  exact\r\nlast  \r\n"),
    ("typed", "  exact\r\nlast  \r\n", "exact\r\nlast"),
])
async def test_submit_retains_exact_rpc_text_and_existing_typed_trimming(skill_app, monkeypatch, source, value, expected):
    app = skill_app
    admitted = []
    app.watch_pending_image = lambda _value: None
    app.pending_image = None
    monkeypatch.setattr(hook_host, "start_prompt", lambda _app, prompt: admitted.append(prompt))
    app._user_bubble = lambda *_args, **_kwargs: None
    app._scroll_down = lambda **_kwargs: None
    app._submit_text(value, False, source=source)
    assert len(admitted) == 1 and admitted[0]["content"] == expected
    assert admitted[0]["source"] == source


@pytest.mark.asyncio
@pytest.mark.parametrize("value, command", [("  /settings \r\n", "/settings"), (" \r\n\t ", None)])
async def test_exact_rpc_text_still_refuses_empty_and_routes_padded_slash(skill_app, monkeypatch, value, command):
    app = skill_app
    routed, refused, admitted = [], [], []
    app.watch_pending_image = lambda _value: None
    app.pending_image = None
    app._handle_command = lambda text: routed.append((text, app._command_source))
    app._refuse_submit = lambda reason, detail: refused.append(reason)
    monkeypatch.setattr(hook_host, "start_prompt", lambda *_args: admitted.append(True))
    before = tree(app)
    app._submit_text(value, False, source="rpc")
    assert routed == ([(command, "rpc")] if command else [])
    assert refused == ([] if command else ["empty"])
    assert admitted == [] and tree(app) == before

@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [True, False])
async def test_selected_relative_path_cannot_rebind_after_working_directory_changes(skill_app, tmp_path, monkeypatch, relative):
    app = skill_app
    left, right = tmp_path / "left", tmp_path / "right"
    for directory, content in ((left, b"original"), (right, b"different")):
        (directory / "relative").mkdir(parents=True)
        (directory / "relative/SKILL.md").write_bytes(content)
    monkeypatch.chdir(left)
    selected = Path("relative/SKILL.md") if relative else left / "relative/SKILL.md"
    app.skills = [skills.Skill("relative", "Exact selected location", selected, "fixture")]
    catalog = command_surface.catalog(app, {"session_id": app.convo_id})
    row = next(row for row in catalog["entries"] if row["kind"] == "skill")
    request = {key: catalog[key] for key in ("version", "session_id", "agent_id", "backend", "model", "snapshot")}
    request.update(row_id=row["id"], arguments="")
    monkeypatch.chdir(right)
    refreshed = command_surface.catalog(app, {"session_id": app.convo_id})
    refreshed_row = next(row for row in refreshed["entries"] if row["kind"] == "skill")
    assert (refreshed_row["id"] == row["id"]) is (not relative)
    with monkeypatch.context() as patch:
        def no_read(_path):
            raise AssertionError("A stale selection must refuse before any file read")
        patch.setattr(native_skills, "_read", no_read)
        with pytest.raises(ValueError, match="stale"):
            native_skills.prepare(app, request)
    if not relative:
        # CWD is already part of tool policy, so even absolute selections need
        # a fresh catalog; their exact selected-file identity remains unchanged.
        result = native_skills.prepare(app, {**request, "snapshot": refreshed["snapshot"]})
        assert result["prompt"].endswith("original")
        assert result["file"]["resolved_path"] == str(selected.resolve())

@pytest.mark.asyncio
async def test_prepared_response_budget_counts_full_ascii_envelope_at_exact_boundary(skill_app, tmp_path, monkeypatch):
    app = skill_app
    _path, request = setup(app, tmp_path)
    request["id"] = "escaped \U0001f680"
    before = tree(app)
    result = native_skills.prepare(app, request)
    size = len(json.dumps({"type": "response", "ok": True, "id": request["id"], "result": result}, default=str).encode())
    monkeypatch.setattr(native_skills, "MAX_RESPONSE_BYTES", size)
    assert native_skills.prepare(app, request) == result
    monkeypatch.setattr(native_skills, "MAX_RESPONSE_BYTES", size - 1)
    with pytest.raises(ValueError, match="wire budget"):
        native_skills.prepare(app, request)
    assert tree(app) == before


@pytest.mark.asyncio
async def test_valid_large_escaped_response_within_wire_budget_remains_read_only(skill_app, tmp_path):
    app = skill_app
    _path, request = setup(app, tmp_path, b"\0" * 60000)
    before = tree(app)
    result = native_skills.prepare(app, request)
    size = len(json.dumps({"type": "response", "ok": True, "result": result}).encode())
    assert 524288 < size <= 1048576
    assert len(result["prompt"].encode()) <= 65536 and tree(app) == before
