"""The plugin substrate gains an OBSERVER table: plugins can watch host events.

Why this exists. The registry had four tables — tools, commands, palette rows,
prompt sections — and every one of them is a plugin telling the host "offer this
to the user". None of them let a plugin WATCH. The glass-box brain needs exactly
that and nothing else: it contributes no tool and no command, it only wants to
know when the host thought, called something, wrote something, or finished a
turn. Adding a fifth table is cheaper and far less invasive than threading a
telemetry callback through the stream loop.

THE TWO PROPERTIES THAT ARE NOT OPTIONAL, and why each is tested here:

  1. unload() must sweep observers. This file's own docstring says the registry
     is built PER INSTANCE precisely so one test's app cannot leak into the
     next. A table that unload() forgets re-opens that leak, and it re-opens it
     SILENTLY — the symptom is a later test receiving events from a dead app.
     Every other table is swept; forgetting one is this repo's most repeated
     defect shape.

  2. A raising observer must not break emit(). Observers sit inside the stream
     loop. If a telemetry plugin can throw and take the turn down with it, then
     installing the brain makes the app less reliable than not having it — and
     the failure would arrive as a broken conversation, with nothing pointing at
     the observer. Telemetry is allowed to fail. It is not allowed to be load
     bearing.

Every test below fails against the pre-feature registry: `observe` and `emit`
do not exist, so they raise AttributeError. That is the negative control, and
it was run and seen red before a line of the feature was written.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from plugins import PluginContext, PluginRegistry


def _ctx(reg: PluginRegistry, owner: str) -> PluginContext:
    return PluginContext(app=None, registry=reg, owner=owner)


def test_an_observer_receives_an_emitted_event() -> None:
    reg = PluginRegistry()
    seen: list[dict] = []
    _ctx(reg, "glassbox").observe(seen.append)

    reg.emit({"channel": "thinking", "intensity": 0.5, "label": "x"})

    assert seen == [{"channel": "thinking", "intensity": 0.5, "label": "x"}]


def test_every_observer_receives_every_event() -> None:
    """Two plugins may watch the same stream; neither consumes it."""
    reg = PluginRegistry()
    a: list[dict] = []
    b: list[dict] = []
    _ctx(reg, "glassbox").observe(a.append)
    _ctx(reg, "auditor").observe(b.append)

    reg.emit({"channel": "output", "intensity": 1.0, "label": ""})

    assert len(a) == 1 and len(b) == 1, "an observer swallowed the event"


def test_emit_with_no_observers_is_silent() -> None:
    """The overwhelming common case: nobody is watching. It must cost nothing
    and must not raise, because this runs inside the stream loop."""
    PluginRegistry().emit({"channel": "output", "intensity": 1.0, "label": ""})


def test_a_raising_observer_does_not_break_the_turn() -> None:
    """THE load-bearing test. A telemetry plugin that throws must not take the
    stream down with it, and the observers registered after it must still run."""
    reg = PluginRegistry()
    survived: list[dict] = []

    def explodes(_event: dict) -> None:
        raise RuntimeError("telemetry plugin is broken")

    _ctx(reg, "broken").observe(explodes)
    _ctx(reg, "healthy").observe(survived.append)

    reg.emit({"channel": "tool_call", "intensity": 1.0, "label": "bash"})

    assert survived, (
        "a raising observer suppressed a later one — one broken telemetry "
        "plugin must not blind the rest"
    )


def test_unload_sweeps_observers() -> None:
    """Every other table is swept by unload(). This one must be too, or a
    disabled plugin keeps receiving events and a per-test app leaks into the
    next one."""
    reg = PluginRegistry()
    seen: list[dict] = []
    _ctx(reg, "glassbox").observe(seen.append)

    reg.unload("glassbox")
    reg.emit({"channel": "ledger", "intensity": 1.0, "label": "done"})

    assert seen == [], "unload() left the observer table populated"


def test_unload_sweeps_only_its_own_owner() -> None:
    """The negative control for the test above: a sweep that removed everything
    would also pass it. This is the case that separates 'swept correctly' from
    'cleared the table'."""
    reg = PluginRegistry()
    doomed: list[dict] = []
    kept: list[dict] = []
    _ctx(reg, "glassbox").observe(doomed.append)
    _ctx(reg, "auditor").observe(kept.append)

    reg.unload("glassbox")
    reg.emit({"channel": "ledger", "intensity": 1.0, "label": "done"})

    assert doomed == [], "the unloaded plugin still received an event"
    assert len(kept) == 1, "unload() swept an owner it was not asked to sweep"


def test_the_observer_table_is_per_registry() -> None:
    """The module docstring's stated reason for per-instance registries. A
    module-global observer list would leak one app's telemetry into another."""
    first, second = PluginRegistry(), PluginRegistry()
    seen: list[dict] = []
    _ctx(first, "glassbox").observe(seen.append)

    second.emit({"channel": "output", "intensity": 1.0, "label": ""})

    assert seen == [], "an observer registered on one registry fired on another"


def test_an_observer_cannot_forge_another_plugins_identity() -> None:
    """The owner is closed over by the context, exactly as it is for every
    other registration. A plugin cannot unload a rival by claiming its name."""
    reg = PluginRegistry()
    seen: list[dict] = []
    _ctx(reg, "honest").observe(seen.append)

    reg.unload("impostor")
    reg.emit({"channel": "output", "intensity": 1.0, "label": ""})

    assert len(seen) == 1, "unloading an unrelated owner removed a live observer"
