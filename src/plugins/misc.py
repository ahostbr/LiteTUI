"""Small host-surface commands: clear-screen, thinking level, quit.

Handler bodies moved verbatim from the _handle_command chain (self -> app).
Also owns the Toggle-agent-tools palette row — one of the two rows that are
not slash-commands, kept via the explicit palette_row escape hatch so the
derived palette stays lossless.
"""
from settings import THINKING_LEVELS

from plugins import PluginManifest


def _cmd_clear_screen(app, name: str, arg: str) -> None:
    # The DISPLAY only. /clear resets the conversation; this does not.
    app._clear_screen(
        note="Screen cleared. The conversation is unchanged - the model "
             "still has everything it had a moment ago."
    )


def _cmd_think(app, name: str, arg: str) -> None:
    if not arg:
        current = app.thinking_level or "unset"
        note = (
            "\nunset means the field is not sent at all — LM Studio then "
            "applies its OWN default, which is xhigh. 'unset' is not 'off'."
        )
        app._system(
            f"Thinking level: {current}\n"
            f"Levels: {', '.join(THINKING_LEVELS)}, or 'unset'\n"
            f"Usage: /think <level>{note}"
        )
    elif arg.lower() in ("unset", "default", "server"):
        app.thinking_level = None
        app._update_header()
        app._system("Thinking level unset — LM Studio's default (xhigh) applies.")
    elif arg.lower() in THINKING_LEVELS:
        app.thinking_level = arg.lower()
        app._update_header()
        wire = "none" if app.thinking_level == "off" else app.thinking_level
        app._system(f"Thinking level: {app.thinking_level} (sends reasoning_effort={wire!r})")
    else:
        app._system(
            f"Unknown level: {arg}\nValid: {', '.join(THINKING_LEVELS)}, unset"
        )


def _cmd_quit(app, name: str, arg: str) -> None:
    app.exit()


def _register(ctx) -> None:
    app = ctx.app
    ctx.command(
        ("/clear-screen", "/clearscreen", "/cls"), _cmd_clear_screen,
        palette="Clear screen",
        help="Display only — the conversation is untouched (/clear-screen)",
    )
    ctx.command(
        ("/think", "/thinking"), _cmd_think,
        palette="Thinking level",
        help="Show the levels and the current one (/think)",
    )
    ctx.command(("/quit", "/exit"), _cmd_quit)
    ctx.palette_row(
        "Toggle agent tools",
        "bash, read, write and friends on/off (Ctrl+T)",
        app.action_toggle_tools,
    )


PLUGIN = PluginManifest(id="misc", register=_register)
