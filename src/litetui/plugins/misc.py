"""Small host-surface commands: clear-screen, thinking level, quit.

Handler bodies moved verbatim from the _handle_command chain (self -> app).
Also owns the Toggle-agent-tools palette row — one of the two rows that are
not slash-commands, kept via the explicit palette_row escape hatch so the
derived palette stays lossless.
"""
from litetui.settings import THINKING_LEVELS

from litetui.plugins import PluginManifest


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
        app.system_message(
            f"Thinking level: {current}\n"
            f"Levels: {', '.join(THINKING_LEVELS)}, or 'unset'\n"
            f"Usage: /think <level>{note}"
        )
    elif arg.lower() in ("unset", "default", "server"):
        app.thinking_level = None
        app.update_header()
        app.system_message("Thinking level unset — LM Studio's default (xhigh) applies.")
    elif arg.lower() in THINKING_LEVELS:
        app.thinking_level = arg.lower()
        app.update_header()
        wire = "none" if app.thinking_level == "off" else app.thinking_level
        app.system_message(f"Thinking level: {app.thinking_level} (sends reasoning_effort={wire!r})")
    else:
        app.system_message(
            f"Unknown level: {arg}\nValid: {', '.join(THINKING_LEVELS)}, unset"
        )


def _cmd_quit(app, name: str, arg: str) -> None:
    app.exit()


# Four rows Textual used to own. They are ours now so they can carry a group,
# a description and a slash command like everything else in the palette.
def _cmd_tools(app, name: str, arg: str) -> None:
    app.action_toggle_tools()


def _cmd_keys(app, name: str, arg: str) -> None:
    app.action_show_help_panel()


def _cmd_maximize(app, name: str, arg: str) -> None:
    app.screen.action_maximize()


def _cmd_screenshot(app, name: str, arg: str) -> None:
    app.action_screenshot()


def _register(ctx) -> None:
    app = ctx.app
    ctx.command(
        ("/clear-screen", "/clearscreen", "/cls"), _cmd_clear_screen,
        palette="Clear screen",
        help="Tidies the view. Your conversation is kept.",
        group="screen",
        order=10,
    )
    ctx.command(
        ("/think", "/thinking"), _cmd_think,
        palette="Thinking level",
        help="How hard it thinks before answering. More thinking, better answers, slower replies.",
        group="backend",
        order=70,
    )
    ctx.command(
        ("/quit", "/exit"), _cmd_quit,
        palette="Quit",
        help="Close the app.",
        group="app",
        order=90,          # last row of the last group, away from anything frequent
    )
    # Was a palette_row, which is the escape hatch for things that have no
    # command. It has one now, so it goes through the front door.
    ctx.command(
        ("/tools",), _cmd_tools,
        palette="Toggle agent tools",
        help="Let it read files, run commands and edit things. Ctrl+T.",
        group="tools",
        order=10,
    )
    ctx.command(
        ("/keys",), _cmd_keys,
        palette="Keys",
        help="Which keys do what right now.",
        group="app",
        order=30,
    )
    ctx.command(
        ("/screenshot",), _cmd_screenshot,
        palette="Screenshot",
        help="Save a picture of the app to disk.",
        group="screen",
        order=30,
    )
    ctx.command(
        ("/maximize",), _cmd_maximize,
        palette="Maximize",
        help="Make the focused panel fill the window.",
        group="screen",
        order=40,
    )


PLUGIN = PluginManifest(id="misc", register=_register)
