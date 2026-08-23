"""/mark — the human screen-marker channel's command surface.

The machinery (_start_mark, the worker group, ttyguard.popen envelope)
stays app-owned; this is the command that starts it.
"""
from litetui.plugins import PluginManifest


def _cmd_mark(app, name: str, arg: str) -> None:
    app._start_mark()


def _register(ctx) -> None:
    ctx.command(
        ("/mark",), _cmd_mark,
        palette="Mark the screen",
        help="Drag a ring over anything on screen and send it, so it can see what you mean.",
        group="screen",
        order=20,
    )


PLUGIN = PluginManifest(id="mark", register=_register)
