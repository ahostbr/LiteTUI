"""/mark — the human screen-marker channel's command surface.

The machinery (_start_mark, the worker group, ttyguard.popen envelope)
stays app-owned; this is the command that starts it.
"""
from plugins import PluginManifest


def _cmd_mark(app, name: str, arg: str) -> None:
    app._start_mark()


def _register(ctx) -> None:
    ctx.command(
        ("/mark",), _cmd_mark,
        palette="Mark the screen",
        help="Draggable ring; send returns screenshot + coords (/mark)",
    )


PLUGIN = PluginManifest(id="mark", register=_register)
