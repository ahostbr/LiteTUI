"""/mark — the human screen-marker channel.

A human drags a ring onto the thing, clicks send; the overlay writes a PNG of
that monitor WITH THE RING STILL IN IT plus a JSON handoff; LiteTUI ships
both to the model as a user turn. The ring staying in the shot is the whole
feature — it is the highlight.
"""
from pathlib import Path
from types import SimpleNamespace

from app import LiteTUI

REPO = Path(__file__).resolve().parent.parent
APP_SRC = (REPO / "src" / "app.py").read_text(encoding="utf-8")
PS1 = (REPO / "tools" / "pccontrol" / "marker_overlay.ps1").read_text(encoding="utf-8")

DATA = {"x": 2408, "y": 1311, "mon": 0, "mon_x": 2408, "mon_y": 1311,
        "png": "C:/tmp/mark.png"}


# --- the message the model receives ------------------------------------------
def test_mark_message_carries_both_coordinate_systems():
    """Absolute for pccontrol's global path, monitor-local for --mon N — an
    agent acting on the mark needs whichever its tool takes."""
    text = LiteTUI._mark_message(DATA)
    assert "(2408,1311)" in text
    assert "monitor 0" in text
    assert "ring" in text            # tells the model the highlight is visible


# --- the overlay script ------------------------------------------------------
def test_overlay_keeps_its_timed_clickthrough_mode():
    """The agent path (pccontrol marker) must be untouched: -Interactive is
    additive. Click-through + auto-close survive for the default mode."""
    assert "WS_EX_TRANSPARENT" in PS1
    assert "$timer.Start()" in PS1
    assert "[switch]$Interactive" in PS1


def test_overlay_ring_stays_in_the_screenshot():
    """Only the BUTTONS hide before capture. Hiding the ring would ship a
    screenshot with the highlight removed — a mark with no mark."""
    hide = PS1.index("$btnSend.Visible = $false")
    shot = PS1.index("CopyFromScreen")
    assert hide < shot
    assert "$f.Visible = $false" not in PS1
    assert "$f.Hide()" not in PS1


def test_overlay_handoff_is_atomic():
    """Write-then-rename: the poller must never read a torn JSON."""
    assert '"$HandoffFile.tmp"' in PS1
    assert "Move-Item -Force" in PS1


# --- wiring ------------------------------------------------------------------
def test_mark_route_exists_and_waits_outside_the_chat_group():
    # The route lives in the registry since the plugin split.
    import app as m
    assert "/mark" in m.LiteTUI().plugins.commands, "/mark is not registered"
    body = APP_SRC.split("async def _mark_wait", 1)[0]
    assert '@work(exclusive=True, group="mark")' in APP_SRC.split(
        "def _start_mark", 1)[1], \
        "the wait must not share the chat group — it would cancel turns"


def test_mark_result_respects_the_midturn_hold():
    """Mid-turn, the mark queues like inbox mail instead of cancelling the
    running turn; idle, it sends immediately."""
    wait_body = APP_SRC.split("async def _mark_wait", 1)[1].split(
        "\n    def _handle_command", 1)[0]
    assert "_pending_input.append" in wait_body
    assert "self._chat_running()" in wait_body
