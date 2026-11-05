"""Current-session Context pages exercise real owned storage without launching a provider."""
from __future__ import annotations

import hashlib
import json

import pytest
import pytest_asyncio
from test_rust_background_input import app as owned_app
from test_rust_background_input import command
from test_rust_background_input import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import native_context
from litetui.agent_store import StoreError

app, switch_app = owned_app, owned_switch_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def armed(app):
    app._launch_options.work_lifecycle_version = 1
    captured = work.capture(app, command(app, type="gui.work.native_capture"))
    work.activate(app, command(app, type="gui.work.native_activate", instance=captured["instance"],
                               activation_token=captured["activation_token"]))
    return app


def request(app, document="system", **fields):
    return {"version": 1, "id": "read-context", "type": native_context.OPERATION,
            **work.identity(app), "work_instance": work.bridge(app).instance,
            "document": document, "offset": 0, "revision": None, **fields}


async def test_plugin_view_preserves_load_order_error_style_and_registry(armed, monkeypatch):
    from litetui import runtime_log

    def unexpected(*args, **kwargs):
        pytest.fail("Plugin inspection logged, executed or enumerated a tool provider")

    monkeypatch.setattr(runtime_log, "record_error", unexpected)
    monkeypatch.setattr(armed.plugins, "tool_specs", unexpected)
    armed.plugins.status = {"second": "active", "first": "failed: Error\nmore"}
    armed.settings.error_message_style = "plain"
    before = armed.convo_path.read_bytes()
    page = native_context.page(armed, request(armed, "plugins"))
    lines = page["text"].splitlines()
    assert len(lines) == 3 and lines[1] == '"second"  "active"'
    assert lines[2].startswith('"first"  "A plugin could not start')
    armed.settings.error_message_style = "detail"
    stale = native_context.page(armed, request(armed, "plugins", revision=page["revision"]))
    assert stale["status"] == "changed"
    detail = native_context.page(armed, request(armed, "plugins"))
    assert detail["text"].splitlines()[-1] == '"first"  "failed: Error\\nmore"'
    assert armed.convo_path.read_bytes() == before
    armed.plugins.status = {"too-large": "a" * (native_context.MAX_DOCUMENT + 1)}
    assert native_context.page(armed, request(armed, "plugins"))["status"] == "oversize"


async def test_exact_current_prompt_and_no_rebuild(armed, monkeypatch):
    def forbidden(*_args):
        pytest.fail("Reading the actual system row rebuilt or mutated it")
    monkeypatch.setattr(armed, "_system_prompt_text", forbidden)
    monkeypatch.setattr(armed, "_edit", forbidden)
    before = armed.convo_path.read_bytes()
    result = native_context.page(armed, request(armed))
    expected = armed.conversation[0]["content"]
    assert result == {"version": 1, "kind": "context_page", **work.identity(armed),
        "work_instance": work.bridge(armed).instance, "document": "system", "status": "page",
        "revision": hashlib.sha256(expected.encode()).hexdigest(), "total_bytes": len(expected.encode()),
        "offset": 0, "next_offset": None, "text": expected}
    assert armed.convo_path.read_bytes() == before


async def test_byte_pages_round_trip_exact_maximum_newlines_and_unicode(armed):
    target = armed._agent_session.memory_root / "memory.md"
    content = ("q" * 4094 + "🦉\r\n" + "\tβ" * 3000).encode()
    content += b"z" * (native_context.MAX_DOCUMENT - len(content))
    target.write_bytes(content)
    command = request(armed, target.name)
    pages = []
    while True:
        result = native_context.page(armed, command)
        assert result["status"] == "page" and result["revision"] == hashlib.sha256(content).hexdigest()
        data = result["text"].encode()
        assert result["offset"] == sum(map(len, pages))
        pages.append(data)
        if result["next_offset"] is None:
            break
        assert 4093 <= len(data) <= 4096 and result["next_offset"] == sum(map(len, pages))
        command.update(revision=result["revision"], offset=result["next_offset"])
    assert b"".join(pages) == content and target.read_bytes() == content
    with pytest.raises(ValueError, match="splits"):
        native_context.page(armed, {**command, "offset": 4095})


async def test_missing_empty_oversize_and_revision_drift_are_distinct(armed):
    target = armed._agent_session.memory_root / "soul.md"
    target.unlink(missing_ok=True)
    command = request(armed, target.name)
    assert native_context.page(armed, command)["status"] == "missing" and not target.exists()
    target.write_bytes(b"")
    empty = native_context.page(armed, command)
    assert (empty["status"], empty["total_bytes"], empty["revision"]) == ("empty", 0, hashlib.sha256(b"").hexdigest())
    target.write_bytes(b"A" * (native_context.MAX_DOCUMENT + 1))
    assert native_context.page(armed, command)["status"] == "oversize"
    stale = {**command, "revision": empty["revision"]}
    for contents in (None, b"replaced"):
        if contents is None:
            target.unlink()
        else:
            target.write_bytes(contents)
        result = native_context.page(armed, stale)
        assert result["status"] == "changed" and result["revision"] is None and not result["text"]
    target.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        native_context.page(armed, command)


async def test_reject_foreign_owner_extra_keys_arbitrary_paths_and_offsets(armed):
    for bad in ({"document": "memories/nested.md"}, {"document": "../memory.md"}, {"document": "C:/other"},
                {"document": []}, {"path": "untrusted"}, {"version": True}, {"offset": True}, {"offset": -1},
                {"offset": 1}, {"revision": "F" * 64}, {"work_instance": "x"}, {"agent_id": "y"}):
        with pytest.raises(ValueError):
            native_context.page(armed, {**request(armed), **bad})
    work.begin_close(armed)
    with pytest.raises(ValueError):
        native_context.page(armed, request(armed))


async def test_owner_override_never_reads_other_memory(armed, tmp_path, monkeypatch):
    monkeypatch.setattr(armed, "_owner_memory_dir", tmp_path)
    for name in ("memory.md", "soul.md", "handoff.md"):
        with pytest.raises(ValueError, match="Owner memory"):
            native_context.page(armed, request(armed, name))
    assert native_context.page(armed, request(armed))["status"] == "page"


async def test_linked_memory_is_refused(armed, tmp_path):
    target = armed._agent_session.memory_root / "handoff.md"
    target.unlink(missing_ok=True)
    other = tmp_path / "outside-note"
    other.write_text("private fixture", encoding="utf-8")
    try:
        target.symlink_to(other)
    except OSError as exc:
        pytest.skip(f"Test filesystem cannot create a symbolic link: {exc}")
    with pytest.raises(StoreError, match="Linked"):
        native_context.page(armed, request(armed, target.name))


async def test_hooks_show_effective_scope_without_execution_details(armed, tmp_path, monkeypatch):
    from litetui import lifecycle_hooks as hooks

    config = hooks.HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    monkeypatch.setattr(armed, "hook_config", config)
    monkeypatch.delenv("LITETUI_HOOKS", raising=False)
    command = request(armed, "hooks")
    locks = set(tmp_path.glob("*.json.lock"))
    assert "No lifecycle hooks" in native_context.page(armed, command)["text"]
    assert set(tmp_path.glob("*.json.lock")) == locks

    hook = {"id": "check", "events": ["tool_before"], "mode": "gate", "executable": "private-executable",
            "argv": ["private-argument"], "env": {"KEY": "private-credential"}, "tools": ["private-filter"]}
    config.global_path.write_text(json.dumps({"version": 1, "hooks": [hook]}), encoding="utf-8-sig")
    config.project_path.write_text(json.dumps({"version": 1, "hooks": [
        {**hook, "enabled": False, "sources": ["rpc"]},
        {**hook, "id": "second\nentry", "mode": "observe"}]}), encoding="utf-8")
    before = config.project_path.read_bytes()
    page = native_context.page(armed, command)
    assert 'project/"check"  disabled  gate' in page["text"] and "global/" not in page["text"]
    assert r'second\nentry' in page["text"] and "Sources: rpc" in page["text"] and "Events: tool_before" in page["text"]
    assert "Tool filters: 1" in page["text"] and "private-" not in page["text"]
    assert config.project_path.read_bytes() == before and set(tmp_path.glob("*.json.lock")) == locks
    config.project_path.unlink()
    assert native_context.page(armed, {**command, "revision": page["revision"]})["status"] == "changed"
    assert 'global/"check"  enabled  gate' in native_context.page(armed, command)["text"]
    monkeypatch.setenv("LITETUI_HOOKS", "off")
    config.global_path.write_bytes(b"invalid json")
    assert "hooks are off" in native_context.page(armed, command)["text"]


async def test_hooks_reject_malformed_linked_and_oversize_config(armed, tmp_path, monkeypatch):
    from litetui import lifecycle_hooks as hooks

    config = hooks.HookConfig(tmp_path / "global.json", tmp_path / "project.json")
    monkeypatch.setattr(armed, "hook_config", config)
    monkeypatch.delenv("LITETUI_HOOKS", raising=False)
    command = request(armed, "hooks")
    config.global_path.write_bytes(b"bad private text")
    with pytest.raises(ValueError, match="could not be inspected"):
        native_context.page(armed, command)
    config.global_path.write_text('{"version": 1, "hooks": [{}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid lifecycle hook configuration"):
        native_context.page(armed, command)
    config.global_path.write_bytes(b"x" * (native_context.MAX_DOCUMENT + 1))
    assert native_context.page(armed, command)["status"] == "oversize"
    config.global_path.unlink()
    outside = tmp_path / "sensitive"
    outside.write_bytes(b"private outside content")
    config.global_path.hardlink_to(outside)
    with pytest.raises(ValueError, match="could not be inspected"):
        native_context.page(armed, command)


async def test_negotiated_rpc_uses_the_canonical_page(armed, monkeypatch):
    from test_rust_scheduled_publication import published_rpc

    hello = await published_rpc(armed, monkeypatch, {"id": "context-hello", "type": "gui.hello", "protocol_version": 1})
    assert native_context.OPERATION in hello["operations"] and "context.native_page" in hello["capabilities"]
    command = request(armed)
    assert await published_rpc(armed, monkeypatch, command) == native_context.page(armed, command)


async def test_hardlinked_foreign_memory_is_refused(armed, tmp_path):
    target = armed._agent_session.memory_root / "memory.md"
    target.unlink(missing_ok=True)
    other = tmp_path / "outside-hardlink"
    other.write_bytes(b"foreign fixture")
    target.hardlink_to(other)
    with pytest.raises(ValueError, match="regular unlinked"):
        native_context.page(armed, request(armed, target.name))
