"""/mark — the human screen-marker channel.

The overlay launch (`start_mark`) lives HERE since S3; `_mark_wait` and the
`@work(group="mark")` envelope stay app-owned, so this file still reaches one
app private and says so rather than hiding it.

⚠️ This docstring used to read "The machinery (_start_mark, the worker group,
ttyguard.popen envelope) stays app-owned". S3 made the first of those three
false, and a docstring naming a member that no longer lives where it says is
how a phantom member is born -- so it moved with the body rather than after it.
"""
import subprocess
import tempfile
from pathlib import Path

from litetui import paths, ttyguard
from litetui.plugins import PluginManifest

# Derived from paths.ROOT, exactly as app.py:120 derives it -- NOT imported from
# app.py, which a plugin cannot import without a cycle. Two derivations of one
# fact exist only until the app.py copy is removed.
MARK_SCRIPT = paths.ROOT / "tools" / "pccontrol" / "marker_overlay.ps1"


def start_mark(app) -> None:
    """/mark — the human screen-marker channel.

    Spawns the interactive marker overlay (draggable ring + send/cancel),
    then polls for its handoff file. The overlay writes the JSON and a PNG
    of the marked monitor WITH THE RING STILL IN THE SHOT — the ring is
    the highlight; that is the whole feature.
    """
    if not MARK_SCRIPT.exists():
        app.system_message(f"/mark: overlay script missing at {MARK_SCRIPT}")
        return
    handoff = Path(tempfile.mkdtemp(prefix="litetui_mark_")) / "mark.json"
    try:
        # No -Label: a spaced label dies through some launch paths, and
        # the ring is self-explanatory. Keep the HANDLE — a timeout must
        # take the unanswered ring down, not leave it as screen litter.
        proc = ttyguard.popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(MARK_SCRIPT),
             "-Interactive", "-HandoffFile", str(handoff),
             "-Color", "cyan"],
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        app.system_message(f"/mark: could not launch the overlay: {e}")
        return
    app.system_message(
        "Marker up — drag the ring onto the thing, then click send. "
        "(x or Esc cancels; times out in 3 minutes.)"
    )
    app._mark_wait(handoff, proc)


def _cmd_mark(app, name: str, arg: str) -> None:
    start_mark(app)


def _register(ctx) -> None:
    ctx.command(
        ("/mark",), _cmd_mark,
        palette="Mark the screen",
        help="Drag a ring over anything on screen and send it, so it can see what you mean.",
        group="screen",
        order=20,
    )


PLUGIN = PluginManifest(id="mark", register=_register)
