"""The consumer: a durable JSONL record and a live SSE feed for the brain page.

wiring.md's Tier 1 ends at "the brain page polls/SSE-consumes and calls
GlassBoxBrain.fire(...)". Two transports, because they answer different
questions:

  JSONL   the durable record. Costs no port, has no failure mode worth
          worrying about, and is what you read AFTER a turn to ask what
          happened. Always on once the plugin is enabled.
  SSE     the live feed, which is the only one that can animate anything. The
          brain page is served from file://, so its fetch carries Origin: null
          and the emitter MUST send Access-Control-Allow-Origin: * or the
          browser drops the response before any JS sees it. That header is not
          decoration; without it the feature is inert and the page shows
          nothing with no error in the app.

🔴 THE PROPERTY THIS FILE EXISTS TO PIN: A BROWSER MUST NEVER BE ABLE TO STALL
THE TUI. Subscribers are fed through bounded queues and a full one is DROPPED,
not waited on. A user who leaves the brain page open on a backgrounded tab, or
closes the laptop lid, must not freeze the agent mid-turn. Dropping frames from
an ambient visualisation is free; blocking the stream loop is not.
"""

from __future__ import annotations

import json
import queue
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui.plugins import PluginContext, PluginRegistry
from litetui.plugins import glassbox_plugin as gb
from litetui import appsvc


def _event(channel="output", intensity=1.0, label="x") -> dict:
    return {"channel": channel, "intensity": intensity, "label": label}


# ── the hub: fan-out that cannot block ──────────────────────────────────────
def test_a_subscriber_receives_a_published_event() -> None:
    hub = gb.Hub()
    q = hub.subscribe()
    hub.publish(_event())
    assert q.get_nowait()["channel"] == "output"


def test_every_subscriber_receives_every_event() -> None:
    hub = gb.Hub()
    a, b = hub.subscribe(), hub.subscribe()
    hub.publish(_event())
    assert a.get_nowait() and b.get_nowait(), "one subscriber consumed the event"


def test_a_full_subscriber_is_dropped_rather_than_waited_on() -> None:
    """THE load-bearing one. publish() runs on the app's thread, inside the
    stream loop. A browser that has stopped reading must cost frames, not the
    turn."""
    hub = gb.Hub()
    q = hub.subscribe()
    for _ in range(gb.QUEUE_MAX + 25):
        hub.publish(_event())          # must not raise, must not block

    assert q.qsize() <= gb.QUEUE_MAX, "the bound was exceeded"


def test_publishing_with_no_subscribers_is_silent() -> None:
    gb.Hub().publish(_event())


def test_unsubscribe_stops_delivery() -> None:
    hub = gb.Hub()
    q = hub.subscribe()
    hub.unsubscribe(q)
    hub.publish(_event())
    assert q.empty(), "an unsubscribed client still received events"


def test_a_subscriber_that_vanishes_does_not_break_the_others() -> None:
    hub = gb.Hub()
    doomed, kept = hub.subscribe(), hub.subscribe()
    hub.unsubscribe(doomed)
    hub.publish(_event())
    assert kept.get_nowait(), "removing one subscriber blinded another"


# ── the durable record ──────────────────────────────────────────────────────
def test_each_event_appends_one_valid_json_line(tmp_path: Path) -> None:
    rec = gb.Recorder(tmp_path / "gb.jsonl")
    rec.write(_event(channel="thinking"))
    rec.write(_event(channel="ledger", label="5 → 3"))

    lines = (tmp_path / "gb.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, f"expected one line per event, got {len(lines)}"
    parsed = [json.loads(x) for x in lines]
    assert [p["channel"] for p in parsed] == ["thinking", "ledger"]
    assert all("ts" in p for p in parsed), "no timestamp — the record is not a record"


def test_a_unicode_label_survives_the_round_trip(tmp_path: Path) -> None:
    """The ledger label carries → and −. A recorder that mangles them writes a
    file that reads back as a different event."""
    rec = gb.Recorder(tmp_path / "gb.jsonl")
    rec.write(_event(channel="ledger", label="47 → 3 · −74%"))
    back = json.loads((tmp_path / "gb.jsonl").read_text(encoding="utf-8"))
    assert back["label"] == "47 → 3 · −74%"


def test_an_unwritable_path_does_not_raise(tmp_path: Path) -> None:
    """Recording is best-effort: it runs inside the stream loop, and a full
    disk must not end the turn."""
    rec = gb.Recorder(tmp_path / "nope" / "deeper" / "gb.jsonl")
    rec.path = tmp_path / "\x00illegal"     # unopenable on every platform
    rec.write(_event())                      # must not raise


# ── registration ────────────────────────────────────────────────────────────
class _App:
    def __init__(self):
        self.plugins = PluginRegistry()


def test_registering_installs_an_observer_that_reaches_the_hub(tmp_path) -> None:
    app = _App()
    ctx = PluginContext(app=app, registry=app.plugins, owner="glassbox")
    gb.register(ctx, root=tmp_path)
    gb._ENABLED = True   # off by default now; /glassbox start flips it

    q = gb.HUB.subscribe()
    try:
        app.plugins.emit(_event(channel="tool_call", label="bash"))
        got = q.get_nowait()
        assert got["channel"] == "tool_call" and got["label"] == "bash"
    finally:
        gb.HUB.unsubscribe(q)


def test_the_observer_is_swept_by_unload(tmp_path) -> None:
    app = _App()
    ctx = PluginContext(app=app, registry=app.plugins, owner="glassbox")
    gb.register(ctx, root=tmp_path)
    gb._ENABLED = True
    app.plugins.unload("glassbox")

    q = gb.HUB.subscribe()
    try:
        app.plugins.emit(_event())
        assert q.empty(), "a disabled glassbox plugin still fed the hub"
    finally:
        gb.HUB.unsubscribe(q)


# ── the CORS header, which is the whole difference between live and inert ───
def test_the_sse_response_allows_a_null_origin() -> None:
    """The brain page is file://, so its Origin is `null`. Without a wildcard
    ACAO the browser discards the stream and the page silently shows nothing —
    no error in the app, no error in the page's own logs unless you look."""
    headers = gb.sse_headers()
    assert headers.get("Access-Control-Allow-Origin") == "*", (
        "a file:// page cannot read this feed"
    )
    assert headers.get("Content-Type", "").startswith("text/event-stream")
    assert headers.get("Cache-Control") == "no-cache"


def test_an_event_serialises_as_one_sse_frame() -> None:
    frame = gb.sse_frame(_event(channel="ledger", label="47 → 3"))
    assert frame.startswith("data: "), f"not an SSE frame: {frame!r}"
    assert frame.endswith("\n\n"), "an SSE frame must end with a blank line"
    assert json.loads(frame[len("data: "):].strip())["label"] == "47 → 3"


# ── the half that is easy to ship dead ───────────────────────────────────────
def test_the_plugin_is_in_the_load_order() -> None:
    """A plugin file that nothing loads is the same as no plugin. Discovery is
    the explicit tuple, not the filesystem, so a module can sit complete and
    tested on disk and never run."""
    from litetui.plugins import PLUGIN_LOAD_ORDER
    assert "litetui.plugins.glassbox_plugin" in PLUGIN_LOAD_ORDER


def test_a_real_app_ends_up_with_a_glassbox_observer() -> None:
    """The end-to-end wiring check, and the one that closes the loop this
    project keeps leaving open: the module exists, it is in the load order, its
    register() runs, AND the running app's registry actually holds the
    observer. Four places; asserting fewer is how half a feature ships."""
    from litetui import app as app_mod
    from litetui.settings import Settings

    a = app_mod.LiteTUI()
    a.settings = Settings()
    owners = {o.owner for o in a.plugins.observers}
    assert "glassbox" in owners, (
        f"the plugin loaded but registered no observer; owners={owners}"
    )


def test_the_running_app_publishes_a_real_channel_to_the_hub() -> None:
    """And the last link: an app-side _glassbox call reaches the hub a browser
    would be subscribed to. Everything before this proves a component works."""
    from litetui import app as app_mod
    from litetui.settings import Settings

    a = app_mod.LiteTUI()
    a.settings = Settings()
    gb._ENABLED = True
    q = gb.HUB.subscribe()
    try:
        a._glassbox_tool("bash")
        got = q.get_nowait()
        assert got["channel"] == "tool_call" and got["label"] == "bash"
    finally:
        gb.HUB.unsubscribe(q)
