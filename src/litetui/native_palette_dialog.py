"""The command palette for the native frontend: Textual's own CommandPalette, run headless.

Python's header icon (and the palette's Theme row) open Textual's CommandPalette: a search
input over a fuzzy-ranked list of LiteTUI's commands (LiteTUICommands); the Theme row opens
the same palette over the themes (App.search_themes, ThemeProvider), where a pick sets
app.theme (watch_theme saves it) and closes.

As for /cal and /modelcfg, the real palette runs in the headless app, fitted to the
frontend's terminal, and every ranking, highlight and scroll is Python's. ``act`` sets the
typed search (the palette re-ranks in its own worker), moves the highlight with the
palette's own bindings, or picks: on a theme, or on the Theme row, Python's own selection
runs; on any other command the palette closes and the answer names the picked row
("route"), which the frontend opens through its own native route for that row.
"""
from __future__ import annotations

import asyncio

from litetui.native_calendar_dialog import _fit, _refreshed, _region, _restore, _size
from litetui.native_hooks_editor import _check, _settle

VERSION = 1
PREFIX = "gui.palette.native_dialog_"
OPERATIONS = tuple(PREFIX + name for name in ("open", "act", "read", "close"))
MAX_QUERY = 512

#: The palette's own bindings for each key the frontend forwards.
_KEYS = {"down": "cursor_down", "up": "command_list('cursor_up')",
         "pagedown": "command_list('page_down')", "pageup": "command_list('page_up')",
         "escape": "escape"}


def _palette(app):
    from textual.command import CommandPalette

    screen = getattr(app, "_native_palette_screen", None)
    if screen is not None and screen in app.screen_stack and isinstance(screen, CommandPalette):
        return screen
    return None


def _rich(style) -> str:
    """A Rich Style as its own spec ("bold #e0e0e0 on #141f27"), which the frontend parses."""
    return "" if style is None else str(style)


def _runs(strip) -> list:
    """A rendered Strip as {"text", "style"} runs: Textual's own cells, colours resolved."""
    return [{"text": seg.text, "style": _rich(seg.style)} for seg in strip if seg.text]


def _edge(widget, edge: str) -> str:
    """A border edge's glyph style as Textual draws it: the border colour over the widget's
    composited background."""
    _kind, color = getattr(widget.styles, "border_" + edge)
    base = widget.background_colors[1]
    return f"{(base + color).hex6} on {base.hex6}".lower()


def _options(listing) -> list:
    """Every option as Textual renders it (OptionList.render_line's styles: padding 0 2, the
    bold option text, the muted help, the blurred block cursor on the highlighted row)."""
    out = []
    for index in range(listing.option_count):
        option = listing.get_option_at_index(index)
        if option.disabled:
            style = listing.get_visual_style("option-list--option", "option-list--option-disabled")
        elif listing.highlighted == index:
            style = listing.get_visual_style("option-list--option", "option-list--option-highlighted")
        else:
            style = listing.get_visual_style("option-list--option")
        out.append([_runs(strip) for strip in listing._get_option_render(option, style)])
    return out


def _themes(screen) -> bool:
    from textual.command import CommandInput

    return str(screen.query_one(CommandInput).placeholder).startswith("Search for themes")


def snapshot(app) -> dict:
    from textual.command import CommandInput, CommandList, SearchIcon

    screen = _palette(app)
    if screen is None:
        return {"version": VERSION, "kind": "palette_dialog", "screen": "closed",
                "theme": str(app.theme)}
    box = screen.query_one("#--input")
    field = screen.query_one(CommandInput)
    icon = screen.query_one(SearchIcon)
    backdrop = screen.styles.background
    out = {"version": VERSION, "kind": "palette_dialog", "screen": "themes" if _themes(screen) else "commands",
           "size": list(app.size), "theme": str(app.theme),
           "box": _region(box), "icon": _region(icon), "field": _region(field),
           "text": [field.content_region.x, field.content_region.y, field.content_region.width, 1],
           "value": field.value, "cursor": field.cursor_position, "placeholder": str(field.placeholder),
           # Textual's own colours for the active theme: `background: $background 60%` over
           # the screen behind, the box and its hkey rule, the input's text, placeholder, cursor.
           "paint": {"backdrop": backdrop.hex6.lower(),
                     "backdrop_alpha": round(float(backdrop.a), 4),
                     "box": _rich(box.rich_style), "rule": _edge(box, "top"),
                     "text": _rich(field.rich_style),
                     "placeholder": _rich(field.get_component_rich_style("input--placeholder")),
                     "cursor": _rich(field.get_component_rich_style("input--cursor")),
                     "icon": _rich(icon.rich_style)},
           "list": None}
    listing = screen.query_one(CommandList)
    if listing.has_class("--visible") and listing.option_count:
        content = listing.scrollable_content_region
        out["list"] = {
            "region": _region(listing),
            "content": [content.x, content.y, content.width, content.height],
            "scroll_y": int(listing.scroll_offset.y), "max_scroll_y": int(listing.max_scroll_y),
            "bar": _region(listing.vertical_scrollbar) if listing.show_vertical_scrollbar else None,
            "bar_rows": ([_runs(listing.vertical_scrollbar.render_line(y))
                          for y in range(listing.vertical_scrollbar.size.height)]
                         if listing.show_vertical_scrollbar else []),
            "rule": _edge(listing, "bottom"),
            "highlighted": listing.highlighted,
            "options": _options(listing),
        }
    return out


async def _quiet(app, screen) -> None:
    """Until the palette's search worker has ranked the typed text (or a half second)."""
    await _settle(app)
    for _ in range(50):
        busy = [w for w in screen.workers if not w.is_finished]
        if not busy and screen.has_class("-ready"):
            break
        await asyncio.sleep(0.01)
    for _ in range(2):
        await _refreshed(app)
    try:
        await asyncio.wait_for(app.animator.wait_until_complete(), 1.0)
    except asyncio.TimeoutError:
        pass
    await _settle(app)


def _themes_was(screen) -> bool:
    """Whether a palette (possibly closed now) was the theme search."""
    try:
        return _themes(screen)
    except Exception:  # noqa: BLE001 - a dismissed screen may have lost its children
        return False


def _hit(screen):
    from textual.command import CommandList

    listing = screen.query_one(CommandList)
    if listing.highlighted is None:
        return None
    return getattr(listing.get_option_at_index(listing.highlighted), "hit", None)


async def _pick(app, screen):
    """Enter on the highlighted row. Returns the routed row's text, or None when Python ran it."""
    hit = _hit(screen)
    if hit is None:
        return None
    runs_here = _themes(screen) or hit.command == app.action_change_theme
    if runs_here:
        # A theme, or the Theme row (it opens the theme search): Python's own selection.
        screen._action_command_list("select")
        return None
    # Any other command: the frontend opens it natively; the palette just closes.
    text = str(hit.text)
    await screen.run_action("escape")
    return text


async def dispatch(app, cmd):
    from textual.command import CommandInput, CommandList

    operation = cmd["type"]
    if operation == PREFIX + "open":
        _check(cmd, ("size", "mode"))
        if cmd["mode"] not in ("commands", "themes"):
            raise ValueError("Palette mode is commands or themes")
        await _fit(app, _size(cmd))
        if _palette(app) is None:
            # The header icon (commands), or the Theme row's own action (the theme search).
            if cmd["mode"] == "themes":
                app.action_change_theme()
            else:
                app.action_command_palette()
            await _settle(app)
            app._native_palette_screen = app.screen
            if _palette(app) is None:
                await _restore(app)
                raise ValueError("The command palette did not open")
            await _quiet(app, app.screen)
        return snapshot(app)
    if operation == PREFIX + "read":
        _check(cmd, ("size",))
        await _fit(app, _size(cmd))
        return snapshot(app)
    if operation == PREFIX + "act":
        _check(cmd, ("size", "values", "action"))
        await _fit(app, _size(cmd))
        screen = _palette(app)
        if screen is None:
            raise ValueError("The command palette is not open")
        values, action = cmd["values"], cmd["action"]
        if not isinstance(values, dict) or set(values) - {"query"}:
            raise ValueError("Palette values are malformed")
        if "query" in values:
            query = values["query"]
            if not isinstance(query, str) or len(query.encode()) > MAX_QUERY or "\0" in query:
                raise ValueError("Palette search is malformed")
            field = screen.query_one(CommandInput)
            if field.value != query:
                field.value = query
                field.cursor_position = len(query)
        await _quiet(app, screen)
        routed = None
        if isinstance(action, dict) and set(action) == {"key"} and action["key"] in _KEYS:
            await screen.run_action(_KEYS[action["key"]])
        elif isinstance(action, dict) and set(action) == {"option"}:
            listing = screen.query_one(CommandList)
            index = action["option"]
            if type(index) is not int or not 0 <= index < listing.option_count:
                raise ValueError("That row is not offered")
            listing.highlighted = index
            routed = await _pick(app, screen)
        elif isinstance(action, dict) and set(action) == {"key"} and action["key"] == "enter":
            routed = await _pick(app, screen)
        elif action is not None:
            raise ValueError("Palette action is malformed")
        await _settle(app)
        # The Theme row opens another palette (the theme search) through call_later: wait
        # for it (bounded), then follow it.
        from textual.command import CommandPalette

        theme_row = routed is None and _palette(app) is None and not _themes_was(screen)
        for _ in range(50 if theme_row else 0):
            if isinstance(app.screen, CommandPalette) and app.screen is not screen:
                break
            await asyncio.sleep(0.01)
        if _palette(app) is None and isinstance(app.screen, CommandPalette):
            app._native_palette_screen = app.screen
        top = _palette(app)
        if top is not None:
            await _quiet(app, top)
        else:
            app._native_palette_screen = None
            await _restore(app)
        out = snapshot(app)
        if routed is not None:
            out["routed"] = routed
        return out
    if operation == PREFIX + "close":
        _check(cmd, ())
        screen = _palette(app)
        app._native_palette_screen = None
        if screen is not None and app.screen is screen:
            await screen.run_action("escape")
            await _settle(app)
        await _restore(app)
        return {"version": VERSION, "closed": True, "theme": str(app.theme)}
    raise ValueError("Unknown palette operation")
