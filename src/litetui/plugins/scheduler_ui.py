"""The scheduler's screens: the month grid, the day view, the job builder.

Moved WHOLE from app.py in the plugin split — screen internals unchanged,
including their (deliberately broad) access to app.jobs and _fire_job.
The app's CSS still styles these by class name; the house centering
selector includes them, and the POSITION gate proves it.
"""
from datetime import date
from datetime import datetime
from textual import events
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import OptionList
from textual.widgets import Select
from textual.widgets import Static
from textual.widgets import Switch
from textual.widgets.option_list import Option
from litetui import calendar_view as calview
from litetui import scheduler as sched_mod
from litetui import schedule_builder as sb_mod
from litetui.ticker import NumberTicker
from rich.text import Text
from litetui import paths
from litetui.tool_policy import INTERACTIVE, SCHEDULED


def _theme_palette(app) -> dict:
    """Resolve theme variables to real colours for Rich.

    `$error` and friends are TEXTUAL CSS variables. Rich parses style strings
    itself and has never heard of them -- it raises MissingStyle, which
    surfaces as the whole screen refusing to render rather than as a wrong
    colour. Caught by the live screen tests; no source check could have seen
    it, because a style string is only parsed when something actually paints.

    Falls back to plain styles rather than raising: a calendar in the wrong
    colours is worth having, one that will not open is not. Module-level
    because three screens now share it -- the grid, the day popup, and the
    job editor must disagree about nothing.
    """
    try:
        theme = app.get_theme(app.theme)
    except Exception:
        theme = None
    if theme is None:
        return {"title": "bold", "dayname": "", "weekend": "bold",
                "today": "bold reverse", "event": "", "muted": "dim",
                "broken": "bold", "off": "strike dim"}
    return {
        "title":   f"bold {theme.error}",
        "dayname": f"{theme.primary}",
        "weekend": f"{theme.error}",
        "today":   f"bold {theme.success}",
        "event":   f"{theme.accent}",
        "muted":   f"dim {theme.foreground}",
        "broken":  f"bold {theme.error}",
        "off":     f"strike dim {theme.foreground}",
    }


class _CalendarGrid(Static):
    """The month grid, and nothing else — except that it forwards clicks.

    The widget stays a drawing; the SCREEN owns the hit-map, because the
    screen is what painted the drawing. Coordinates are widget-relative,
    which is exactly what the map is keyed in.
    """

    def on_click(self, event: events.Click) -> None:
        screen = self.screen
        if isinstance(screen, CalendarScreen):
            screen.grid_clicked(event.x, event.y)


class _CalendarSide(Static):
    """The side pane. A click on a job's three lines opens its editor."""

    def on_click(self, event: events.Click) -> None:
        screen = self.screen
        if isinstance(screen, CalendarScreen):
            screen.side_clicked(event.y)


class CalendarScreen(ModalScreen[None]):
    """A month of cron jobs, drawn the way calcure draws a month.

    The layout maths are calcure's: cell width is the pane divided by seven,
    six week rows, the day number at the top-left of its cell, a `\u2502` rule
    and a side pane. What changed is the paint -- Textual, so it runs on
    Windows, and theme variables instead of fixed ANSI colours so it follows
    whichever SHADES theme is on rather than fighting it.

    One Static per pane, each painting a Rich Text sized to the space it has.
    Forty-two cell widgets would reflow more prettily and would also make the
    grid a layout problem; this keeps it a drawing problem, which is what it is.
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
        Binding("left,h", "prev_month", "Prev", show=False),
        Binding("right,l", "next_month", "Next", show=False),
        Binding("t", "today", "Today", show=False),
    ]

    def __init__(self, jobs: list):
        super().__init__()
        self._jobs = jobs
        today = date.today()
        self._year, self._month = today.year, today.month
        # Hit-maps, rebuilt by every paint. Empty until the first one, so a
        # click that somehow lands before on_mount resolves to nothing.
        self._click_rows: list = []      # y -> week index, or None
        self._matrix: list = []          # the painted month_matrix
        self._cell_w: int = 1            # painted cell width
        self._side_rows: list = []       # y -> job id, or None

    def compose(self) -> ComposeResult:
        with Vertical(id="cal-box"):
            with Horizontal(id="cal-body"):
                yield _CalendarGrid(id="cal-grid")
                yield _CalendarSide(id="cal-side")
            yield Static(id="cal-hints")

    def on_mount(self) -> None:
        self._paint()

    def on_resize(self, _event) -> None:
        # The grid is drawn to fit, so a resize is a repaint, not a reflow.
        self._paint()

    # -- painting ---------------------------------------------------------

    def _palette(self) -> dict:
        return _theme_palette(self.app)

    def _paint(self) -> None:
        self.query_one("#cal-grid", Static).update(self._grid_text())
        self.query_one("#cal-side", Static).update(self._side_text())
        self.query_one("#cal-hints", Static).update(self._hints_text())

    def _grid_text(self) -> Text:
        grid = self.query_one("#cal-grid", Static)
        pal = self._palette()
        width = max(42, grid.size.width or 70)
        cell = max(6, width // 7)

        # calcure's own maths: the pane, minus the title and day-name rows,
        # divided by six week rows; one line of that is the day number itself.
        # A fixed cap made nearly every cell read "+n more" on a tall terminal,
        # which is honest and useless — seeing what is on a day IS the feature.
        height = grid.size.height or 30
        per_cell = max(1, (height - 2) // calview.WEEK_ROWS - 1)

        today = date.today()
        # no_wrap is load-bearing, not cosmetic: a soft-wrap in a narrow pane
        # would shear every mapped y below the fold. Cropping keeps the map
        # honest at any width.
        out = Text(no_wrap=True)

        # THE HIT-MAP IS BUILT BESIDE THE PAINT — one entry per emitted line,
        # appended in the same statement group that emits the line. Clicks
        # need y -> week and x -> column; deriving them here, from the loop
        # that draws, is what makes drift impossible. A parallel bookkeeping
        # pass would agree with the paint right up until someone edited one
        # of them.
        rows_map: list = []
        matrix = calview.month_matrix(self._year, self._month)

        title = f"{calview.MONTHS[self._month - 1]} {self._year}"
        out.append(title + "\n", style=pal["title"])
        rows_map.append(None)

        # Full names when they fit, the three-letter form when they do not.
        # Truncating to the cell width is what calcure does and it is fine at a
        # wide terminal; at cell=11 it yields WEDNE and THURS, which are not
        # words. Falling back to MON/TUE keeps it readable at any width.
        longest = max(len(d) for d in calview.DAYS)
        abbreviate = cell < longest + 1
        for i, name in enumerate(calview.DAYS):
            label = name[:3] if abbreviate else name
            style = pal["weekend"] if calview.is_weekend(i) else pal["dayname"]
            out.append(label[: cell - 1].ljust(cell), style=style)
        out.append("\n")
        rows_map.append(None)

        for wk, week in enumerate(matrix):
            # The day-number row, then one row per event line beneath it --
            # exactly calcure's stacking inside a cell.
            rows: list[list[Text]] = []
            numbers = Text()
            for col, day in enumerate(week):
                if not day:
                    numbers.append(" " * cell)
                    continue
                is_today = (day == today.day and self._month == today.month
                            and self._year == today.year)
                if is_today:
                    style = pal["today"]
                    label = f"{day}{calview.ICON_TODAY}"
                elif calview.is_weekend(col):
                    style, label = pal["weekend"], str(day)
                else:
                    style, label = "", str(day)
                numbers.append(label.ljust(cell), style=style)
            rows.append(numbers)

            # Up to two event lines per cell keeps a month on one screen.
            cells: list[list[calview.DayEntry]] = []
            for col, day in enumerate(week):
                if not day:
                    cells.append([])
                    continue
                cells.append(
                    calview.entries_for(self._jobs, date(self._year, self._month, day))
                )

            # cell_lines declares overflow as `+n more` rather than dropping
            # it. A calendar that silently hides an entry will tell you the day
            # is free, which is the one thing it must never get wrong.
            per_cell_lines = [calview.cell_lines(e, limit=per_cell) for e in cells]
            for line in range(per_cell):
                if not any(len(ls) > line for ls in per_cell_lines):
                    break
                row = Text()
                for entries, lines in zip(cells, per_cell_lines):
                    if len(lines) <= line:
                        row.append(" " * cell)
                        continue
                    text = lines[line][: cell - 1].ljust(cell)
                    overflow = lines[line].endswith("more") and len(entries) > len(lines)
                    entry = entries[line] if line < len(entries) else None
                    if overflow:
                        row.append(text, style=pal["dayname"])
                    elif entry is not None and entry.broken:
                        row.append(text, style=pal["broken"])
                    elif entry is not None and not entry.enabled:
                        # Dimmed AND struck, calcure's treatment for done: still
                        # on the calendar, plainly not going to happen.
                        row.append(text, style=pal["off"])
                    else:
                        row.append(text, style=pal["event"])
                rows.append(row)

            for row in rows:
                out.append_text(row)
                out.append("\n")
                rows_map.append(wk)
            # The blank spacer belongs to the week above it: a click just
            # under a cell's last line is aimed at that cell, and a bigger
            # target is the point of mapping regions instead of digits.
            out.append("\n")
            rows_map.append(wk)

        self._click_rows = rows_map
        self._matrix = matrix
        self._cell_w = cell
        return out

    def _side_text(self) -> Text:
        pal = self._palette()
        out = Text(no_wrap=True)
        # Same discipline as the grid: the y -> job-id map grows beside the
        # append that paints the line it describes. Lines past the end of the
        # map (the totals, the warning) resolve to None, which is correct —
        # they are not jobs.
        side_map: list = []
        out.append("JOBS\n", style=pal["title"])
        side_map.append(None)

        cron_jobs = [j for j in self._jobs if getattr(j, "kind", "cron") == "cron"]
        if not cron_jobs:
            out.append("\nnothing scheduled\n", style=pal["muted"])
            out.append("\n/cron add @daily <prompt>\n", style=pal["muted"])
            self._side_rows = side_map
            return out

        for job in cron_jobs:
            try:
                nxt = job.next_after(datetime.now())
                when = nxt.strftime("%a %d %H:%M") if nxt else "never"
                broken = False
            except Exception:
                when, broken = "unparseable", True

            if broken:
                icon, style = calview.ICON_DEADLINE, pal["broken"]
            elif not job.enabled:
                icon, style = calview.ICON_DONE, pal["off"]
            else:
                icon, style = calview.ICON_IMPORTANT, pal["event"]

            out.append(f"{icon} ", style=style)
            out.append(f"{(job.label or job.prompt)[:26]}\n", style=style)
            out.append(f"   {job.schedule}\n", style=pal["muted"])
            out.append(f"   next {when}\n", style=pal["muted"])
            side_map.extend([job.id, job.id, job.id])   # all three lines

        self._side_rows = side_map
        total = calview.month_totals(self._jobs, self._year, self._month)
        out.append(f"\n{total:,} fires this month\n", style=pal["muted"])
        if total > 500:
            # A runaway schedule reads as harmless in a list. Say it here.
            out.append("that is a lot — check for a stray */n\n", style=pal["broken"])
        return out

    def _hints_text(self) -> Text:
        return Text(
            "\u2190 \u2192 month · t today · q close · "
            "click a day to view or add its jobs · click a side-pane job "
            "to edit · jobs fire only while LiteTUI is open",
            style=self._palette()["muted"],
        )

    # -- clicks -----------------------------------------------------------

    def _day_at(self, x: int, y: int):
        """Translate a grid click to a day number, or None for dead space.

        Reads only what the painter recorded. There is no second layout
        computation here to disagree with the first.
        """
        if not (0 <= y < len(self._click_rows)):
            return None
        week = self._click_rows[y]
        if week is None:
            return None
        col = x // self._cell_w
        if col > 6:
            return None                      # remainder columns past Sunday
        day = self._matrix[week][col]
        return day or None                   # 0 = a cell outside the month

    def _job_at(self, y: int):
        """Translate a side-pane click to a job id, or None."""
        if 0 <= y < len(self._side_rows):
            return self._side_rows[y]
        return None

    def grid_clicked(self, x: int, y: int) -> None:
        day = self._day_at(x, y)
        if day:
            self.open_day(day)

    def side_clicked(self, y: int) -> None:
        job_id = self._job_at(y)
        if job_id is None:
            return
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return
        self.app.push_screen(
            JobScreen(job), lambda result, job=job: self._job_edited(job, result)
        )

    def open_day(self, day: int) -> None:
        self.app.push_screen(
            DayScreen(self._jobs, date(self._year, self._month, day)),
            self._refresh_after,
        )

    def _job_edited(self, job, result) -> None:
        _apply_job_edit(self._jobs, job, result)
        self._paint()

    def _refresh_after(self, _result) -> None:
        # Whatever happened in the day popup, the month repaints. A stale
        # month after an edit reads as the edit having been lost.
        self._paint()

    # -- actions ----------------------------------------------------------

    def action_prev_month(self) -> None:
        self._year, self._month = calview.step_month(self._year, self._month, -1)
        self._paint()

    def action_next_month(self) -> None:
        self._year, self._month = calview.step_month(self._year, self._month, 1)
        self._paint()

    def action_today(self) -> None:
        today = date.today()
        self._year, self._month = today.year, today.month
        self._paint()

    def action_close(self) -> None:
        self.dismiss(None)


def _apply_job_edit(jobs: list, job, result) -> bool:
    """The ONE place a UI edit becomes a store mutation. True = changed.

    `result` is a JobScreen dismissal: None (cancelled), ("save", fields),
    or ("delete",). On save, ONLY the edited fields are written — never the
    whole object. The cron monitor may have fired this exact job mid-edit,
    bumping `run_count` and stamping `last_fired_slot` on the live instance;
    clobbering those with the form's stale copy would erase the stamp and
    re-arm the double-fire it exists to prevent.
    """
    if not result:
        return False
    verb = result[0]
    if verb == "delete":
        if job in jobs:
            jobs.remove(job)
    elif verb == "save":
        fields = result[1]
        if job is None:
            jobs.append(sched_mod.Job(**fields))
        else:
            for name, value in fields.items():
                setattr(job, name, value)
    else:
        return False
    try:
        sched_mod.save(jobs, paths.ROOT)
    except OSError:
        pass    # an unwritable store must not lose the in-memory edit
    return True


class DayScreen(ModalScreen[None]):
    """One day of the calendar: its jobs, each a row you can open.

    The list is an OptionList because that is this app's picker idiom
    (PickerScreen) — arrows + enter and mouse both work without any code
    here. The last row is always the create action, so an EMPTY day is not a
    dead end: clicking a free day and adding something to it is the whole
    gesture the calendar exists for.
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
        Binding("n", "new_job", "New job", show=False),
    ]

    def __init__(self, jobs: list, day):
        super().__init__()
        self._jobs = jobs
        self.day = day

    def compose(self) -> ComposeResult:
        # Built from our own constants, not strftime: strftime month names
        # follow the process locale, and the grid this popup came from spells
        # AUGUST from calview.MONTHS. Two spellings of one month is drift.
        title = (f"{calview.DAYS[self.day.weekday()]} {self.day.day} "
                 f"{calview.MONTHS[self.day.month - 1]} {self.day.year}")
        with Vertical(id="day-box"):
            yield Static(title, id="day-title")
            yield OptionList(id="day-list")
            yield Static("enter/click edit · n new · esc close", id="day-hint")

    def on_mount(self) -> None:
        self._refresh()
        self.query_one("#day-list", OptionList).focus()

    def _entries(self):
        entries = calview.entries_for(self._jobs, self.day)
        # Running jobs first in time order, then disabled, then broken.
        # The first draft sorted by time alone -- and a DISABLED 00:00 job
        # outsorted every working one, so the popup led with a struck-out row
        # and Enter edited a job that will not fire. Caught by looking at the
        # render: the ordering test's fixture had no disabled job, so it
        # could not see this. What a day popup ranks is WHAT WILL HAPPEN.
        return sorted(
            entries,
            key=lambda e: (e.broken, not e.enabled,
                           e.times[0] if e.times else "99:99", e.label),
        )

    def _refresh(self) -> None:
        pal = _theme_palette(self.app)
        by_id = {j.id: j for j in self._jobs}
        ol = self.query_one("#day-list", OptionList)
        ol.clear_options()

        entries = self._entries()
        for e in entries:
            job = by_id.get(e.job_id)
            if e.broken:
                style, note = pal["broken"], "schedule does not parse"
            elif not e.enabled:
                style, note = pal["off"], "off"
            else:
                style, note = pal["event"], (job.schedule if job else "")
            label = Text(no_wrap=True)
            label.append(f"{e.icon} ", style=style)
            label.append(e.summary()[:34].ljust(36), style=style)
            label.append(note, style=pal["muted"])
            ol.add_option(Option(label, id=e.job_id))

        if not entries:
            ol.add_option(
                Option(Text("nothing scheduled this day", style=pal["muted"]),
                       disabled=True)
            )
        ol.add_option(Option(f"{calview.ICON_EVENT} new job on this day…", id="new"))

        # Highlight the first selectable row. clear_options() resets the
        # highlight to None, and an OptionList with no highlight swallows
        # Enter whole -- the popup would LOOK ready and select nothing.
        # (PickerScreen, the house idiom, sets highlighted explicitly too.)
        first = next(
            (i for i in range(ol.option_count)
             if not ol.get_option_at_index(i).disabled),
            None,
        )
        ol.highlighted = first

    @on(OptionList.OptionSelected, "#day-list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if oid == "new":
            self.action_new_job()
            return
        job = next((j for j in self._jobs if j.id == oid), None)
        if job is not None:
            self.app.push_screen(
                JobScreen(job), lambda result, job=job: self._job_closed(job, result)
            )

    def action_new_job(self) -> None:
        # "On this day, 09:00" as a starting point. Cron has no year field,
        # so a true one-shot is not expressible — this fires every year on
        # this date, and the editor's live next-fire line SAYS when, which is
        # the guard against the surprise (a 09:00 already past today shows
        # next YEAR, visibly, before saving).
        prefill = f"0 9 {self.day.day} {self.day.month} *"
        self.app.push_screen(
            JobScreen(None, prefill_schedule=prefill),
            lambda result: self._job_closed(None, result),
        )

    def _job_closed(self, job, result) -> None:
        if _apply_job_edit(self._jobs, job, result):
            self._refresh()

    def action_close(self) -> None:
        self.dismiss(None)


class JobScreen(ModalScreen):
    """Edit one cron job, or create one. The form is the whole contract:
    dismisses with None (cancel), ("save", fields), or ("delete",) — the
    caller applies it through _apply_job_edit, never here, so every mutation
    goes through one door no matter which screen opened the editor.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    #: Which param widgets each preset shows. Everything else hides —
    #: including the raw cron field, which only Custom reveals.
    _VIS_TIME = frozenset({"job-lbl-at", "job-hour", "job-lbl-colon", "job-minute"})
    _VIS = {
        "daily":         _VIS_TIME,
        "weekdays":      _VIS_TIME,
        "weekends":      _VIS_TIME,
        "weekly":        _VIS_TIME | {"job-weekday"},
        "monthly":       _VIS_TIME | {"job-lbl-day", "job-day"},
        "yearly":        _VIS_TIME | {"job-month", "job-lbl-day", "job-day"},
        "every_minutes": frozenset({"job-lbl-every", "job-n", "job-unit"}),
        "every_hours":   frozenset({"job-lbl-every", "job-n", "job-unit"}),
        "custom":        frozenset(),
    }
    _PARAM_IDS = tuple(sorted(set().union(*_VIS.values())))

    def __init__(self, job=None, prefill_schedule: str = ""):
        super().__init__()
        self._job = job
        self._prefill = prefill_schedule
        self._delete_armed = False
        # Recognize the schedule into builder state. None routes to CUSTOM
        # with the raw string shown — a recognizer that guessed would let
        # the next save silently rewrite a schedule it never understood.
        initial = job.schedule if job else prefill_schedule
        recognized = sb_mod.recognize(initial)
        self._preset = recognized[0] if recognized else "custom"
        self._p = {**sb_mod.DEFAULTS, **(recognized[1] if recognized else {})}

    def compose(self) -> ComposeResult:
        j = self._job
        with Vertical(id="job-box"):
            yield Static("edit job" if j else "new job", id="job-title")
            yield Static("prompt — sent as a user message when it fires",
                         classes="job-cap")
            yield Input(value=j.prompt if j else "", id="job-prompt")
            yield Static("when it runs", classes="job-cap")
            yield Select(
                [(label, pid) for pid, label in sb_mod.PRESETS],
                value=self._preset, allow_blank=False, id="job-preset",
            )
            with Horizontal(id="job-params"):
                yield Static("at", classes="job-param-label", id="job-lbl-at")
                yield NumberTicker(self._p["hour"], 0, 23, id="job-hour")
                yield Static(":", classes="job-param-label", id="job-lbl-colon")
                yield NumberTicker(self._p["minute"], 0, 59, id="job-minute")
                yield Select(list(sb_mod.WEEKDAYS), value=self._p["weekday"],
                             allow_blank=False, id="job-weekday")
                yield Static("day", classes="job-param-label", id="job-lbl-day")
                yield NumberTicker(self._p["day"], 1, 31, id="job-day")
                yield Select(list(sb_mod.MONTHS), value=self._p["month"],
                             allow_blank=False, id="job-month")
                yield Static("every", classes="job-param-label", id="job-lbl-every")
                yield NumberTicker(self._p["n"], 1, 59, id="job-n")
                yield Static("minutes", classes="job-param-label", id="job-unit")
            yield Static("cron — min hour day month weekday · or @daily @hourly",
                         classes="job-cap", id="job-cron-cap")
            yield Input(value=j.schedule if j else self._prefill, id="job-schedule")
            yield Static("label — short name for lists and cells", classes="job-cap")
            yield Input(value=j.label if j else "", id="job-label")
            with Horizontal(id="job-switches"):
                yield Switch(value=j.enabled if j else True, id="job-enabled")
                yield Static("enabled", classes="job-switchcap")
                yield Switch(value=j.new_conversation if j else False,
                             id="job-newconvo")
                yield Static("fresh conversation", classes="job-switchcap")
            yield Static("tool authority — scheduled is read-only by default",
                         classes="job-cap")
            yield Select(
                [
                    ("scheduled — read-only tools only", SCHEDULED),
                    ("interactive — ask before sensitive tools", INTERACTIVE),
                ],
                value=j.tool_profile if j else SCHEDULED,
                allow_blank=False,
                id="job-tool-profile",
            )
            yield Static(id="job-status")
            with Horizontal(id="job-buttons"):
                yield Button("Save", variant="primary", id="job-save")
                yield Button("Cancel", id="job-cancel")
                if j is not None:
                    yield Button("Delete", variant="error", id="job-delete")

    def on_mount(self) -> None:
        self._sync_params()
        self._preview()
        self.query_one("#job-prompt", Input).focus()

    # -- the builder: widgets in, cron string out -------------------------

    def _sync_params(self) -> None:
        """Show the param widgets this preset uses; hide the rest.

        The raw cron field is a param of exactly one preset — Custom. In
        every other mode it stays in the DOM (the save path reads it, the
        builder writes it) but off the screen.
        """
        preset = self.query_one("#job-preset", Select).value
        visible = self._VIS.get(preset, frozenset())
        for wid in self._PARAM_IDS:
            self.query_one(f"#{wid}").display = wid in visible
        self.query_one("#job-params").display = bool(visible)
        is_custom = preset == "custom"
        self.query_one("#job-cron-cap").display = is_custom
        self.query_one("#job-schedule").display = is_custom
        if preset == "every_minutes":
            self.query_one("#job-unit", Static).update("minutes")
            self.query_one("#job-n", NumberTicker).set_bounds(1, 59)
        elif preset == "every_hours":
            self.query_one("#job-unit", Static).update("hours")
            self.query_one("#job-n", NumberTicker).set_bounds(1, 23)

    def _regen(self) -> None:
        """Rebuild the cron string from the widgets — into the SAME input the
        save path has always read, so downstream code cannot tell the builder
        exists. Fires only on widget events, never on mount: opening a job
        and saving it untouched keeps its exact string, aliases included.
        """
        preset = self.query_one("#job-preset", Select).value
        if preset == "custom":
            return
        self.query_one("#job-schedule", Input).value = sb_mod.build(preset, {
            "hour": self.query_one("#job-hour", NumberTicker).value,
            "minute": self.query_one("#job-minute", NumberTicker).value,
            "weekday": self.query_one("#job-weekday", Select).value,
            "day": self.query_one("#job-day", NumberTicker).value,
            "month": self.query_one("#job-month", Select).value,
            "n": self.query_one("#job-n", NumberTicker).value,
        })

    @on(Select.Changed, "#job-preset")
    def _preset_changed(self, event: Select.Changed) -> None:
        # Textual's Select ECHOES its initial value as a Changed during
        # mount. Guarding by TIMING (a ready flag) races message delivery;
        # guarding by VALUE is deterministic: an event carrying the value we
        # already hold is definitionally not a change. Without this, opening
        # an untouched "@daily" job rewrote it to "0 0 * * *" on save.
        changed = event.value != self._preset
        self._preset = event.value
        self._sync_params()
        if changed:
            self._regen()

    @on(Select.Changed, "#job-weekday")
    @on(Select.Changed, "#job-month")
    def _param_select_changed(self, event: Select.Changed) -> None:
        key = "weekday" if event.control.id == "job-weekday" else "month"
        if event.value == self._p[key]:
            return                      # the mount echo, or a no-op reselect
        self._p[key] = event.value
        self._regen()

    @on(NumberTicker.Changed)
    def _ticker_changed(self, _event) -> None:
        self._regen()

    # -- live schedule feedback -------------------------------------------

    @on(Input.Changed, "#job-schedule")
    def _schedule_changed(self, _event) -> None:
        self._preview()

    def _preview(self) -> bool:
        """Refresh the status line; True when the schedule parses.

        This is the guard that makes the form honest: the person sees the
        NEXT REAL FIRE of what they typed, before saving it — including the
        "next year" a passed date resolves to, and the field name when it
        does not parse at all (CronError already names it).
        """
        status = self.query_one("#job-status", Static)
        pal = _theme_palette(self.app)
        raw = self.query_one("#job-schedule", Input).value
        try:
            cron = sched_mod.Cron.parse(raw)
        except sched_mod.CronError as e:
            status.update(Text(f"{calview.ICON_DEADLINE} {e}", style=pal["broken"]))
            return False
        nxt = cron.next_after(datetime.now())
        when = nxt.strftime("%a %d %b %Y %H:%M") if nxt else "never (no matching date)"
        fires = len(cron.minute) * len(cron.hour)
        note = f" · {fires}\u00d7 per matching day" if fires > 1 else ""
        status.update(Text(f"next: {when}{note}", style=pal["muted"]))
        return True

    # -- exits --------------------------------------------------------------

    def _try_save(self) -> None:
        pal = _theme_palette(self.app)
        status = self.query_one("#job-status", Static)
        prompt = self.query_one("#job-prompt", Input).value.strip()
        if not prompt:
            status.update(Text(
                f"{calview.ICON_DEADLINE} the prompt is empty — nothing would be sent",
                style=pal["broken"]))
            return
        if not self._preview():
            return          # the status line already names the broken field
        self.dismiss(("save", {
            "prompt": prompt,
            "schedule": self.query_one("#job-schedule", Input).value.strip(),
            "label": self.query_one("#job-label", Input).value.strip(),
            "enabled": self.query_one("#job-enabled", Switch).value,
            "new_conversation": self.query_one("#job-newconvo", Switch).value,
            "tool_profile": self.query_one("#job-tool-profile", Select).value,
        }))

    @on(Button.Pressed, "#job-save")
    def _save(self, _event) -> None:
        self._try_save()

    @on(Input.Submitted)
    def _submitted(self, _event) -> None:
        # Enter in any field = attempt save. A form that only saves via a
        # mouse button is not a TUI form.
        self._try_save()

    @on(Button.Pressed, "#job-cancel")
    def _cancel(self, _event) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#job-delete")
    def _delete(self, _event) -> None:
        # Two clicks, the first of which changes the label: a misclick on a
        # destructive button should cost a glance, not a job.
        if not self._delete_armed:
            self._delete_armed = True
            self.query_one("#job-delete", Button).label = "Really delete?"
            return
        self.dismiss(("delete",))
