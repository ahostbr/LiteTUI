"""The scheduler — cron commands, the calendar screens' door, the monitor.

The cron machinery (_cron_command's verbs, _fire_job, the jobs store, the
queued-delivery funnel it feeds) stays app-owned; this plugin owns the
command surfaces, the palette rows, and WHEN the monitor starts. The
screens live in plugins.scheduler_ui, moved whole.
"""
from functools import partial

from litetui import cron as cron_mod
from litetui.plugins import PluginManifest
from litetui.plugins.scheduler_ui import (
    CalendarBody, CalendarScreen, JobBody, JobScreen, _apply_job_edit,
)
from litetui.side_panel import present_dialog


def _cmd_cron(app, name: str, arg: str) -> None:
    app.cron.command(arg)


def _cmd_calendar(app, name: str, arg: str) -> None:
    present_dialog(app, partial(CalendarBody, app.jobs),
                   partial(CalendarScreen, app.jobs))


NEW_JOB_PREFILL = "0 9 * * *"


def _palette_new_job(app) -> None:
    """Create a job from the command palette — no day picked, so the
    builder opens on a sensible daily default instead of a date."""
    present_dialog(
        app,
        partial(JobBody, None, NEW_JOB_PREFILL),
        partial(JobScreen, None, NEW_JOB_PREFILL),
        lambda result: _apply_job_edit(app.jobs, None, result),
    )


def _register(ctx) -> None:
    app = ctx.app
    ctx.command(("/cron",), _cmd_cron)
    ctx.command(
        ("/calendar", "/cal"), _cmd_calendar,
        palette="Calendar",
        help="A month view of everything scheduled.",
        group="automation",
        order=30,
    )
    # Ryan's call: the rows that had no command get one. This was the last
    # palette_row left standing without a way to reach it from the keyboard.
    ctx.command(
        ("/job",), lambda a, name, arg: _palette_new_job(a),
        palette="New scheduled job",
        help="Set something up to run on a timer.",
        group="automation",
        order=20,
    )
    ctx.palette_row(
        "Scheduled jobs",
        "Things set to run on a timer, and when they fire next.",
        lambda: app._handle_command("/cron list"),
        group="automation",
        order=10,
        tag="/cron list",
    )


def _activate(app) -> None:
    cron_mod.monitor(app)


PLUGIN = PluginManifest(id="scheduler", register=_register, activate=_activate)
