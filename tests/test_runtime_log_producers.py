"""Approved T065 producers carry bounded metadata and no bodies."""
from __future__ import annotations

import ast
from pathlib import Path

from litetui import runtime_log
from litetui.conversation import ConversationRepository
from litetui.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parent.parent
PRODUCER_FILES = (
    ROOT / "src" / "litetui" / "app.py",
    ROOT / "src" / "litetui" / "conversation.py",
    ROOT / "src" / "litetui" / "mcp_client.py",
    ROOT / "src" / "litetui" / "plugins" / "__init__.py",
)
EXPECTED_EVENTS = {
    "persistence_failure",
    "harness_registration_failed",
    "harness_rebind_failed",
    "mcp_reader_failed",
    "mcp_config_failed",
    "mcp_server_start_failed",
    "mcp_tool_failed",
    "plugin_observer_failed",
    "plugin_finalizer_failed",
    "plugin_import_failed",
    "plugin_register_failed",
    "plugin_activate_failed",
    "scheduler_tick_failed",
    "backend_connect_failed",
    "turn_stream_failed",
}
PROHIBITED = {
    "prompt",
    "body",
    "content",
    "text",
    "message",
    "arguments",
    "args",
    "result",
    "output",
    "conversation",
    "label",
    "command",
}


def _producer_calls() -> list[ast.Call]:
    calls: list[ast.Call] = []
    for path in PRODUCER_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        calls.extend(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "runtime_log"
            and node.func.attr == "record"
        )
    return calls


def test_exactly_the_approved_producers_exist_and_no_body_key_is_present() -> None:
    calls = _producer_calls()
    assert len(calls) == 16
    events = {
        call.args[0].value
        for call in calls
        if call.args and isinstance(call.args[0], ast.Constant)
    }
    assert events == EXPECTED_EVENTS
    for call in calls:
        keys = {kw.arg for kw in call.keywords}
        assert not keys & PROHIBITED


def test_persistence_failure_records_type_not_error_body(monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_log,
        "record",
        lambda event, **fields: seen.append((event, fields)) or True,
    )
    repo = ConversationRepository()
    repo._raise_to_app(OSError("private path and record body"))
    assert seen == [
        (
            "persistence_failure",
            {
                "site": "conversation.repository",
                "component": "conversation",
                "error_type": "OSError",
            },
        )
    ]
    assert "private path" not in repr(seen)


def test_swallowed_plugin_error_records_owner_and_type_only(monkeypatch) -> None:
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        runtime_log,
        "record",
        lambda event, **fields: seen.append((event, fields)) or True,
    )
    registry = PluginRegistry()

    def broken(_event):
        raise RuntimeError("private observer body")

    registry.add_observer("probe-plugin", broken)
    registry.emit({"channel": "output", "label": "private answer"})
    assert seen == [
        (
            "plugin_observer_failed",
            {
                "site": "plugins.emit",
                "component": "plugin",
                "plugin": "probe-plugin",
                "error_type": "RuntimeError",
            },
        )
    ]
    assert "private observer body" not in repr(seen)
