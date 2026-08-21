"""The scheduler — cron commands, the calendar screens' door, the monitor.

The cron machinery (_cron_command's verbs, _fire_job, the jobs store, the
queued-delivery funnel it feeds) stays app-owned; this plugin owns the
command surfaces, the palette rows, and WHEN the monitor starts. The
screens live in plugins.scheduler_ui, moved whole.
"""
from plugins import PluginManifest
from plugins.scheduler_ui import CalendarScreen, JobScreen, _apply_job_edit


def _cmd_cron(app, name: str, arg: str) -> None:
    app._cron_command(arg)


def _cmd_calendar(app, name: str, arg: str) -> None:
    app.push_screen(CalendarScreen(app._jobs))


def _palette_new_job(app) -> None:
    """Create a job from the command palette — no day picked, so the
    builder opens on a sensible daily default instead of a date."""
    app.push_screen(
        JobScreen(None, prefill_schedule="0 9 * * *"),
        lambda result: _apply_job_edit(app._jobs, None, result),
    )


def _register(ctx) -> None:
    app = ctx.app
    ctx.command(("/cron",), _cmd_cron)
    ctx.command(
        ("/calendar", "/cal"), _cmd_calendar,
        palette="Calendar",
        help="The month, every scheduled job on it (/calendar)",
    )
    ctx.palette_row(
        "New scheduled job",
        "Create a cron job in the builder form",
        lambda: _palette_new_job(app),
    )
    ctx.palette_row(
        "Scheduled jobs",
        "List cron jobs and their next fires (/cron list)",
        lambda: app._handle_command("/cron list"),
    )


def _activate(app) -> None:
    app._cron_monitor()


PLUGIN = PluginManifest(id="scheduler", register=_register, activate=_activate)
