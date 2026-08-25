"""Approved T065 producers carry bounded metadata and no bodies."""
from __future__ import annotations

import ast
from pathlib import Path

from litetui import runtime_log
from litetui.conversation import ConversationRepository
from litetui.plugins import PluginRegistry

ROOT = Path(__file__).resolve().parent.parent

# 🔴 SCAN THE PACKAGE, DO NOT HAND-LIST THE FILES.
#
# This was a four-entry tuple written against the pre-T070 layout. The
# decomposition then moved the scheduler monitor out of `app.py` into `cron.py`,
# and a hand-listed tuple CANNOT SEE a producer that moved to a file it does not
# name: the count silently reads 15, and the cheapest way to make that green is
# to edit the 16 down to 15 — which converts a RELOCATED producer into a DELETED
# one, with a green suite either way.
#
# `app.py` is actively being decomposed, so producers moving between modules is
# the expected case, not an exception to it. Scanning the package means a
# producer that moves stays counted, and only one that is genuinely removed
# changes the number.
PRODUCER_FILES = tuple(sorted((ROOT / "src" / "litetui").rglob("*.py")))
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


def _is_type_of_e_dunder_name(node: ast.AST) -> bool:
    """Is this expression literally `type(<something>).__name__`?"""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    )


def test_every_error_type_is_the_class_name_and_never_the_message() -> None:
    """🔴 STANDING CONSTRAINT 2, ENFORCED STRUCTURALLY INSTEAD OF BY REVIEW.

    The approved list calls this "the single highest-risk line in the whole
    task: it is one character of difference and it silently exfiltrates
    prompts, paths and payloads into an always-on log."

    The key-name check above CANNOT see it. `error_type=str(e)` uses an approved
    key with a forbidden VALUE, so it passes every other assertion in this file
    — measured, not assumed: mutating one producer to `str(e)` left this module
    fully green before this test existed.

    The two behavioural tests below do catch it, but only for the two producers
    they drive (conversation repository, plugins.emit). The other fourteen —
    including all five in app.py and the relocated one in cron.py — had nothing
    checking them at all. This closes that by reading the SOURCE, so it covers
    every producer without needing a driver for each one.
    """
    offenders = [
        (kw.value.lineno, ast.dump(kw.value)[:80])
        for call in _producer_calls()
        for kw in call.keywords
        if kw.arg == "error_type" and not _is_type_of_e_dunder_name(kw.value)
    ]
    assert not offenders, (
        "error_type must be `type(e).__name__`, never the exception message. "
        f"Offending expressions: {offenders}"
    )


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
