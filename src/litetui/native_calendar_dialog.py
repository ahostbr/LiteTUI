"""/cal for the native frontend: Python's own CalendarScreen chain, run headless, as data.

Python's /cal (plugins/scheduler_plugin._cmd_calendar) opens CalendarScreen: the month grid
and the JOBS pane (CalendarBody paints both as Rich Text, with hit-maps built beside the
paint), the hints line and Dock to side. A click on a day opens DayScreen (the day's jobs in
an OptionList and "new job on this day…"); a job, or n there, opens JobScreen (the job
form); each edit goes through scheduler_ui._apply_job_edit, the one door to the store.

As for /hooks and /mcp, the real screens run in the headless app while the frontend's
dialog is open, and every decision is theirs. Here the layout is theirs too: the headless
app (80x24 under --rpc) is resized to the frontend's terminal while the calendar is open,
so the grid is painted for the cells the frontend shows, a click maps through the painter's
own hit-map, and the job form wraps and scrolls as it does on a real 140x45 screen. Each
answer gives the TOP screen's parts with their screen regions: for the month, the grid, the
side pane and the hints as styled lines; for a day or a job, the box and its widgets in
order (native_hooks_editor._item), each with its region, plus the focused widget and the
box's scroll. On close the headless app goes back to its own size.

``open`` pushes CalendarScreen; ``act`` sets edited controls, then sends one key (the
screen's own binding), a click on the grid or the side pane (cells inside those parts), an
option pick, a ticker step, a focus move or a scroll (Textual reveals the focused widget),
a press or a Select pick; ``read`` re-reads; ``close`` pops the whole chain.

present_dialog refuses a dialog over --rpc because no keyboard is attached; while this
session acts, the frontend IS the keyboard, so app._native_dialog_driving lets the chain's
own opens through for that act only.
"""
from __future__ import annotations

import asyncio

from rich.console import Console

from litetui.native_hooks_editor import _check, _item, _set, _settle
from litetui.native_hooks_editor import _action as _widget_action

VERSION = 1
PREFIX = "gui.calendar.native_dialog_"
OPERATIONS = tuple(PREFIX + name for name in ("open", "act", "read", "close"))
MAX_SIZE = 1000

#: Each screen's keys, as its own BINDINGS name them (scheduler_ui _CAL_KEYS/_DAY_KEYS).
_KEYS = {
    "calendar": {"left": "prev_month", "h": "prev_month", "right": "next_month", "l": "next_month",
                 "t": "today", "q": "close", "escape": "close"},
    "day": {"n": "new_job", "q": "close", "escape": "close"},
    "job": {"escape": "cancel"},
}


def _console() -> Console:
    return Console(width=10_000, color_system="truecolor", force_terminal=True, legacy_windows=False)


def _lines(text) -> list:
    """A Rich Text as lines of {"text", "style"} runs (style is Rich's own string)."""
    from rich.text import Text

    if not isinstance(text, Text):
        text = Text(str(text))
    console = _console()
    out = []
    for line in text.split("\n", allow_blank=True):
        runs = []
        for seg in line.render(console):
            if not seg.text:
                continue
            style = str(seg.style) if seg.style else ""
            if runs and runs[-1]["style"] == style:
                runs[-1]["text"] += seg.text
            else:
                runs.append({"text": seg.text, "style": style})
        out.append(runs)
    return out


def _wrapped(text, width: int) -> list:
    """A Static's Rich Text as it shows at `width`: word-wrapped (no_wrap is off)."""
    console = _console()
    out = []
    for line in text.wrap(console, max(1, width)):
        out.extend(_lines(line))
    return out


def _region(widget) -> list:
    r = widget.region
    return [r.x, r.y, r.width, r.height]


def _chain(app) -> list:
    """The calendar chain's screens on the stack, bottom first."""
    from litetui.plugins.scheduler_ui import CalendarScreen, DayScreen, JobScreen

    base = getattr(app, "_native_calendar_screen", None)
    stack = list(app.screen_stack)
    if base is None or base not in stack:
        return []
    return [s for s in stack[stack.index(base):] if isinstance(s, (CalendarScreen, DayScreen, JobScreen))]


def _top(app):
    chain = _chain(app)
    if not chain:
        raise ValueError("The calendar is not open")
    return chain[-1]


def _screen_name(screen) -> str:
    from litetui.plugins.scheduler_ui import CalendarScreen, DayScreen

    if isinstance(screen, CalendarScreen):
        return "calendar"
    if isinstance(screen, DayScreen):
        return "day"
    return "job"


def _size(cmd):
    size = cmd.get("size")
    if (not isinstance(size, list) or len(size) != 2
            or not all(type(n) is int and 10 <= n <= MAX_SIZE for n in size)):
        raise ValueError("The terminal size is malformed")
    return size


async def _fit(app, size) -> None:
    """Lay the headless app out at the frontend's terminal size (kept until close)."""
    from textual import events
    from textual.geometry import Size

    if list(app.size) == list(size):
        return
    if getattr(app, "_native_calendar_restore", None) is None:
        app._native_calendar_restore = tuple(app.size)
    app.post_message(events.Resize(Size(*size), Size(*size)))
    await _settle(app)
    for _ in range(50):
        if list(app.size) == list(size):
            break
        await asyncio.sleep(0.01)
    await _settle(app)


async def _restore(app) -> None:
    """Back to the headless app's own size once the calendar is gone."""
    restore = getattr(app, "_native_calendar_restore", None)
    app._native_calendar_restore = None
    if restore is not None:
        await _fit(app, list(restore))
        app._native_calendar_restore = None


def _painted(screen) -> bool:
    """DayBody and JobBody paint after a refresh (their _first_paint), not on compose."""
    from textual.widgets import OptionList, Static

    name = _screen_name(screen)
    box = screen.query(f"#{'cal' if name == 'calendar' else name}-box")
    if not box or box.first().region.width == 0:
        return False             # composed, not yet laid out
    if name == "day":
        found = screen.query("#day-list")
        return bool(found) and found.first(OptionList).option_count > 0
    if name == "job":
        found = screen.query("#job-status")
        return bool(found) and bool(str(found.first(Static).content))
    return True


async def _refreshed(app) -> None:
    """One refresh of the top screen: what a focus does (scroll its widget into view)
    lands after the refresh that laid the widget out."""
    done = asyncio.get_running_loop().create_future()
    app.screen.call_after_refresh(lambda: done.done() or done.set_result(None))
    try:
        await asyncio.wait_for(done, 0.5)
    except asyncio.TimeoutError:
        pass


async def _settle_chain(app) -> None:
    await _settle(app)
    for _ in range(50):          # at most about half a second
        chain = _chain(app)
        if not chain or _painted(chain[-1]):
            break
        await asyncio.sleep(0.01)
    for _ in range(3):
        await _refreshed(app)
    # A focus scrolls its widget into view with an animated scroll: answer with where it
    # comes to rest, as a person sees it a moment later.
    try:
        await asyncio.wait_for(app.animator.wait_until_complete(), 1.0)
    except asyncio.TimeoutError:
        pass
    await _settle(app)


def _with_regions(widget, entry):
    """_item's entry for `widget`, each widget in it carrying its screen region."""
    if entry is None:
        return None
    entry["region"] = _region(widget)
    if entry["kind"] in ("line", "scroll"):
        shown = [child for child in widget.children if _item(child) is not None]
        entry["items"] = [_with_regions(child, item) for child, item in zip(shown, entry["items"])]
    return entry


def _dock(screen):
    from litetui.side_panel import SwapButton

    found = [b for b in screen.query(SwapButton) if b.display]
    return {"region": _region(found[0]), "label": str(found[0].label)} if found else None


def snapshot(app) -> dict:
    from litetui.plugins.scheduler_ui import CalendarBody, DayBody, JobBody

    chain = _chain(app)
    if not chain:
        return {"version": VERSION, "kind": "calendar_dialog", "screen": "closed"}
    top = chain[-1]
    name = _screen_name(top)
    out = {"version": VERSION, "kind": "calendar_dialog", "screen": name, "size": list(app.size),
           "dock": _dock(top)}
    if name == "calendar":
        body = top.query_one(CalendarBody)
        box = top.query_one("#cal-box")
        parts = {part: top.query_one(f"#cal-{part}") for part in ("grid", "side", "hints")}
        out.update(year=body._year, month=body._month, box=_region(box),
                   grid={"region": _region(parts["grid"]), "lines": _lines(body._grid_text())},
                   side={"region": _region(parts["side"]), "lines": _lines(body._side_text())},
                   hints={"region": _region(parts["hints"]),
                          # padding 0 1: the text is two cells narrower than the region.
                          "lines": _wrapped(body._hints_text(), parts["hints"].region.width - 2)})
        return out
    body = top.query_one(DayBody if name == "day" else JobBody)
    box = body.query_one("#day-box" if name == "day" else "#job-box")
    content = box.scrollable_content_region
    out.update(box=_region(box), content=[content.x, content.y, content.width, content.height],
               scroll_y=int(box.scroll_offset.y), max_scroll_y=int(box.max_scroll_y),
               focused=getattr(app.focused, "id", None),
               items=[e for e in (_with_regions(child, _item(child)) for child in box.children) if e is not None])
    return out


async def _act(app, top, action) -> None:
    from textual.widgets import OptionList

    from litetui.plugins.scheduler_ui import CalendarBody
    from litetui.ticker import NumberTicker

    name = _screen_name(top)
    if not isinstance(action, dict):
        raise ValueError("Calendar action is malformed")
    if set(action) == {"key"}:
        binding = _KEYS[name].get(action["key"])
        if binding is None:
            raise ValueError("That key does nothing here")
        await top.run_action(binding)
        return
    if set(action) in ({"grid_click"}, {"side_click"}) and name == "calendar":
        part = "grid" if "grid_click" in action else "side"
        cell = action[f"{part}_click"]
        if not (isinstance(cell, list) and len(cell) == 2
                and all(type(n) is int and 0 <= n < MAX_SIZE for n in cell)):
            raise ValueError("Calendar click is malformed")
        body = top.query_one(CalendarBody)
        if part == "grid":
            body.grid_clicked(cell[0], cell[1])
        else:
            body.side_clicked(cell[1])
        return
    if set(action) == {"option", "index"} and name == "day":
        options = top.query_one(f"#{action['option']}", OptionList)
        index = action["index"]
        if (type(index) is not int or not 0 <= index < options.option_count
                or options.get_option_at_index(index).disabled):
            raise ValueError("That row is not offered")
        options.highlighted = index
        options.action_select()
        return
    if set(action) == {"tick", "delta"} and name == "job":
        ticker = top.query_one(f"#{action['tick']}", NumberTicker)
        if action["delta"] not in (-1, 1) or not ticker.display:
            raise ValueError("That ticker step is malformed")
        ticker._step(action["delta"])
        return
    if set(action) == {"focus"} and name != "calendar":
        target = top.query_one(f"#{action['focus']}")
        if not target.display or target.disabled:
            raise ValueError("That control cannot take focus")
        from litetui.ticker import NumberTicker as _Ticker

        if isinstance(target, _Ticker):
            from textual.widgets import Input

            target = target.query_one(Input)
        target.focus()
        return
    if set(action) == {"submit"} and name == "job":
        # Enter in any field: Input.Submitted, which JobBody answers with a save attempt.
        from textual.widgets import Input

        target = top.query_one(f"#{action['submit']}")
        if isinstance(target, NumberTicker):
            target = target.query_one(Input)
        if not isinstance(target, Input) or not target.display:
            raise ValueError("That field cannot be submitted")
        await target.action_submit()
        return
    if set(action) == {"scroll"} and name == "job":
        delta = action["scroll"]
        if type(delta) is not int or not -100 <= delta <= 100:
            raise ValueError("Calendar scroll is malformed")
        top.query_one("#job-box").scroll_relative(y=delta, animate=False)
        return
    if name == "calendar":
        raise ValueError("Calendar action is malformed")
    _widget_action(top, action)


async def dispatch(app, cmd):
    from litetui.plugins.scheduler_ui import CalendarScreen

    operation = cmd["type"]
    if operation == PREFIX + "open":
        _check(cmd, ("size",))
        await _fit(app, _size(cmd))
        if not _chain(app):
            # Python's own /cal (present_dialog marks the screen it pushes, which is what
            # shows its Dock to side), with the frontend as the keyboard.
            from litetui.plugins.scheduler_plugin import _cmd_calendar

            app._native_dialog_driving = True
            try:
                _cmd_calendar(app, "/calendar", "")
                await _settle(app)
            finally:
                app._native_dialog_driving = False
            if not isinstance(app.screen, CalendarScreen):
                raise ValueError("The calendar did not open")
            app._native_calendar_screen = app.screen
            await _settle_chain(app)
        return snapshot(app)
    if operation == PREFIX + "read":
        _check(cmd, ("size",))
        await _fit(app, _size(cmd))
        return snapshot(app)
    if operation == PREFIX + "act":
        _check(cmd, ("size", "values", "action"))
        await _fit(app, _size(cmd))
        top = _top(app)
        _set(top, cmd["values"])
        await _settle(app)
        app._native_dialog_driving = True
        try:
            await _act(app, top, cmd["action"])
            await _settle_chain(app)
        finally:
            app._native_dialog_driving = False
        if not _chain(app):
            await _restore(app)      # the month closed itself (q, Escape)
        return snapshot(app)
    if operation == PREFIX + "close":
        _check(cmd, ())
        chain = _chain(app)
        app._native_calendar_screen = None
        for screen in reversed(chain):
            if app.screen is screen:
                app.pop_screen()
                await _settle(app)
        await _restore(app)
        return {"version": VERSION, "closed": True}
    raise ValueError("Unknown calendar operation")
