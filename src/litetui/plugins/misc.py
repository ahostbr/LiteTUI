"""Small host-surface commands: clear-screen, thinking level, quit.

Handler bodies moved verbatim from the _handle_command chain (self -> app).
Also owns the Tools palette row, which used to be a Toggle-agent-tools row and
is now the tool LIST (`/tools`, T076) — the toggle itself stayed on Ctrl+T,
because every tools-off refusal names that key to the user and to the model.
"""
from functools import partial

from litetui.picker import pick
from litetui.settings import THINKING_LEVELS

from litetui.plugins import PluginManifest


def _cmd_clear_screen(app, name: str, arg: str) -> None:
    # The DISPLAY only. /clear resets the conversation; this does not.
    app._clear_screen(
        note="Screen cleared. The conversation is unchanged - the model "
             "still has everything it had a moment ago."
    )


#: What "no level" is called in the picker and in `/think unset`. The field is
#: then not sent at all, which is NOT the same as sending "off".
UNSET = "unset"


def _apply_thinking_level(app, level: str) -> None:
    """Set the level and say what that means. ONE body, two callers.

    🔴 THE PICKER AND `/think <level>` MUST NOT BE TWO IMPLEMENTATIONS. Everything
    below the assignment is caveat: which backend silently ignores graded levels,
    which levels this model actually advertises, what `off` sends on the wire. A
    second copy for the picker would be a second place for those to rot, and the
    way it would show up is a user picking a level from a menu and being told
    less than a user who typed it.
    """
    if getattr(getattr(app, "backend", None), "name", "") == "codex":
        from litetui.thinking_capabilities import set_thinking

        try:
            set_thinking(app, "default" if level == UNSET else level)
        except ValueError as error:
            app.system_message(str(error))
            return
        app.update_header()
        app.system_message(
            "Thinking level: " + (app.thinking_level or "backend default")
            + (" — consumes usage limits faster" if level in ("max", "ultra") else "")
        )
        return
    if level == UNSET:
        app.thinking_level = None
        app.update_header()
        app.system_message("Thinking level unset — LM Studio's default (xhigh) applies.")
        return

    app.thinking_level = level
    app.update_header()
    wire = "none" if app.thinking_level == "off" else app.thinking_level
    msg = f"Thinking level: {app.thinking_level} (sends reasoning_effort={wire!r})"
    backend = getattr(getattr(app, "backend", None), "name", "")
    model_levels = getattr(app, "_model_thinking_levels", None)
    if backend == "lmstudio" and app.thinking_level not in ("off", None):
        if model_levels and app.thinking_level not in model_levels:
            msg += (
                f"\nThis model does not support {app.thinking_level!r} — "
                f"supported levels: {', '.join(model_levels)}. "
                "The level is saved and will apply on llama.cpp."
            )
        elif not model_levels:
            msg += (
                "\nOn the LM Studio backend, graded levels are on/off only — "
                "your level is saved and will apply when you switch to llama.cpp. "
                "'off' is the only real reduction here."
            )
    app.system_message(msg)


def _thinking_rows(app) -> list[tuple[str, str]]:
    """The levels to offer, this model's own list first when it has one.

    ⬜ A MODEL'S LIST IS ALWAYS A SUBSET OF `THINKING_LEVELS` — `thinking_probe`
    builds it from `GRADED_LEVELS` plus "off" — so the picker can never offer a
    value that `/think <level>` would reject. That is asserted in the tests
    rather than assumed here, because it is a property of the probe and the
    probe is free to change.
    """
    if getattr(getattr(app, "backend", None), "name", "") == "codex":
        levels = app.backend.reasoning_levels(app.model_id)
        return [(level, "Extra high" if level == "xhigh" else
                 level.title() + (" · consumes usage limits faster"
                                  if level in ("max", "ultra") else ""))
                for level in levels] + [(UNSET, "Default · use the backend default")]
    model_levels = getattr(app, "_model_thinking_levels", None)
    levels = list(model_levels) if model_levels else list(THINKING_LEVELS)
    rows = [(lv, _level_label(app, lv)) for lv in levels]
    rows.append((UNSET, "unset  · the field is not sent — LM Studio applies xhigh"))
    return rows


def _level_label(app, level: str) -> str:
    if level == "off":
        return "off  · sends reasoning_effort='none'"
    model_levels = getattr(app, "_model_thinking_levels", None)
    if model_levels and level not in model_levels:
        return f"{level}  · not supported by this model"
    return level


def _on_thinking_picked(app, level: str | None) -> None:
    # Esc resolves with None and the callback still fires — see picker.pick.
    if level is None:
        return
    _apply_thinking_level(app, level)


def _cmd_think(app, name: str, arg: str) -> None:
    if not arg:
        if getattr(app, "_rpc", False):
            # 🔴 NO SCREEN OVER RPC (T558-A). A dialog pushed here would wait for
            # a keyboard that is not attached, and the caller — a model, not a
            # person — would hang holding a turn that can never complete. The
            # text output IS the answer on this transport, and there is an arm
            # for it because the failure is invisible from the TUI side.
            _print_thinking_levels(app)
            return
        pick(
            app,
            "Thinking level",
            _thinking_rows(app),
            partial(_on_thinking_picked, app),
            current=app.thinking_level or UNSET,
        )
    elif arg.lower() in (UNSET, "default", "server"):
        _apply_thinking_level(app, UNSET)
    elif arg.lower() in [level for level, _ in _thinking_rows(app)]:
        _apply_thinking_level(app, arg.lower())
    else:
        app.system_message(
            f"Unknown level: {arg}\nValid: {', '.join(level for level, _ in _thinking_rows(app))}"
        )


def _print_thinking_levels(app) -> None:
    """The pre-T569 text listing, kept verbatim for the RPC transport."""
    current = app.thinking_level or UNSET
    if getattr(getattr(app, "backend", None), "name", "") == "codex":
        app.system_message(f"Thinking level: {current}\n" +
                           "\n".join(label for _, label in _thinking_rows(app)))
        return
    note = (
        "\nunset means the field is not sent at all — LM Studio then "
        "applies its OWN default, which is xhigh. 'unset' is not 'off'."
    )
    model_levels = getattr(app, "_model_thinking_levels", None)
    if model_levels:
        levels_str = ", ".join(model_levels)
        note += f"\nThis model supports: {levels_str}"
    app.system_message(
        f"Thinking level: {current}\n"
        f"Levels: {', '.join(THINKING_LEVELS)}, or 'unset'\n"
        f"Usage: /think <level>{note}"
    )


def _cmd_quit(app, name: str, arg: str) -> None:
    app.exit()


# Four rows Textual used to own. They are ours now so they can carry a group,
# a description and a slash command like everything else in the palette.
def _cmd_tools(app, name: str, arg: str) -> None:
    """/tools SHOWS THE TOOL LIST. It used to toggle.

    Ryan, 2026-08-24: "remove the func of /tools switching on and off and make
    it show this list please."

    🔴 Ctrl+T KEEPS THE TOGGLE, deliberately. Every tools-off refusal names it
    to the model and the user ("Ctrl+T, or Settings -> Agent loop"), so
    repointing the command without repointing the key would leave those
    messages pointing at a control that no longer does what they say. The list
    carries its own global switch, and that switch calls the same verb.
    """
    from litetui.side_panel import open_dialog
    from litetui.tool_list import ToolListBody

    # `open_dialog`, not `show_dialog`: this handler is SYNC and show_dialog is
    # a coroutine — the same mismatch OpenBolt measured for three of the four
    # existing dialogs (T078). He added this shape for exactly that, so the
    # bridge is one call rather than a worker spawned here. No callback: every
    # change in the list is already written when it is made, so there is no
    # answer to collect.
    open_dialog(app, ToolListBody)


def _cmd_plan(app, name: str, arg: str) -> None:
    """/plan — plan mode, from the keyboard or the palette (T573 piece 3).

    Ryan asked for this door alongside Ctrl+P and the footer chip (liteask
    a-5d6c1ca0). All three run `set_plan_mode`, which is where entering and
    leaving the mode is actually defined — the prompt section is rebuilt there
    and a second copy of that is the one that forgets it.

    BARE `/plan` TOGGLES, and says which way it landed because `set_plan_mode`
    announces. That is deliberate: the palette row invokes this with no
    argument, and a row that only REPORTS would be a control that does nothing —
    the same defect the footer chip had when it was drawn only while the mode
    was already on. The state is never hidden either way; the chip shows it
    permanently since piece 2.

    `/plan on` and `/plan off` SET rather than toggle, so a script or a second
    invocation cannot flip you into the state you were trying to leave.
    `set_plan_mode` returns False and announces nothing when the value is
    already what you asked for.
    """
    want = (arg or "").strip().lower()
    if want in ("on", "off"):
        app.set_plan_mode(want == "on")
        return
    if want:
        app._system(f"/plan takes no argument, or on/off — not {want!r}")
        return
    app.action_toggle_plan_mode()


def _cmd_keys(app, name: str, arg: str) -> None:
    """/keys TOGGLES the help panel.

    Textual 8.1.0 ships `action_show_help_panel` and `action_hide_help_panel`
    and NO toggle, and the show verb is idempotent by design -- it queries for a
    HelpPanel and mounts one only on NoMatches. So calling it twice opened the
    panel once and then did nothing, which is what Ryan saw. The toggle has to
    be ours; there is nothing upstream to delegate to.
    """
    from textual.widgets import HelpPanel

    if app.screen.query(HelpPanel):
        app.action_hide_help_panel()
    else:
        app.action_show_help_panel()


def _cmd_test_sidebar(app, name: str, arg: str) -> None:
    """/test-sidebar opens the T075 demo dialog THROUGH THE CURRENT SETTING.

    It deliberately takes no argument. An override like `/test-sidebar modal`
    would be quicker for an A/B, but the toggle is itself part of what is being
    evaluated, and a command that can bypass it lets you ship a working demo
    over a broken setting without noticing. Flip `Dialog style` in /settings and
    run this again — that path exercises everything the real dialogs would use.

    The handler has to be sync (app.py:4293 calls handlers directly), so the
    await lives in a worker rather than in this frame.
    """
    from litetui.dialog_demo import DemoDialogBody
    from litetui.side_panel import show_dialog

    # Read the setting HERE, on the live path, rather than leaning on
    # show_dialog's own lookup. test_no_dead_controls scans app.py, plugins/**
    # and llm_backend.py — side_panel.py is NOT in that set, so a read that
    # happens only there is invisible to the dead-control guard and this field
    # would have shipped looking wired while the guard stayed quiet about it.
    # The guard caught exactly that on the first run; this is the fix, not an
    # exemption. (The scan-set gap itself is reported as discovered work.)
    style = app.settings.dialog_style
    side = app.settings.dialog_side

    async def _run() -> None:
        answer = await show_dialog(app, DemoDialogBody, style=style, side=side)
        app.notify(
            f"Demo dialog answered: {answer!r}"
            if answer is not None
            else "Demo dialog cancelled (Esc)",
            timeout=3,
        )

    app.run_worker(_run(), name="test-sidebar")


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
        palette="Tools",
        help="Every tool, with a checkbox each, and one switch for all of them.",
        group="tools",
        order=10,
    )
    ctx.command(
        ("/plan",), _cmd_plan,
        palette="Plan mode",
        help="Plan first, build after. It asks questions instead of writing code.",
        group="backend",
        order=75,          # beside Thinking level, which is the other how-it-works row
    )
    ctx.command(
        ("/keys",), _cmd_keys,
        palette="Keys",
        help="Which keys do what right now.",
        group="app",
        order=30,
    )
    ctx.command(
        ("/test-sidebar",), _cmd_test_sidebar,
        palette="Test sidebar dialog",
        help="T075 spike: open a demo dialog in the current Dialog style.",
        group="app",
        order=31,
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
