"""`/tasks` — the background tasks the agent fired and did not wait for.

The runner (`_start_background`, `_run_background`, `_kill_background`) is
app-owned like `_fire_job`; this plugin owns the command surface only.
"""
from litetui import paths
from litetui import tasks as tasks_mod
from litetui.plugins import PluginManifest


def _cmd_tasks(app, name: str, arg: str) -> None:
    verb, _, rest = (arg or "").strip().partition(" ")
    rest = rest.strip()
    if verb in ("", "list"):
        text = tasks_mod.render_list(app.bg_tasks.values())
        from litetui.task_screens import bg_row

        native = [row for row in tasks_mod.live_for_app(app)[1] if hasattr(row, "thread_id")]
        if native:
            text += "\n\n" + "\n\n".join(bg_row(row) for row in native)
        app._system(text)
    elif verb == "kill" and rest:
        if any(row.id == rest and hasattr(row, "thread_id") for row in tasks_mod.live_for_app(app)[1]):
            app._system("This operation is owned by Codex. Use Stop Codex turn in the activity panel; it stops the whole turn.")
            return
        app._kill_background(rest)
    elif verb == "tail" and rest:
        app._system(tasks_mod.tail_text(app.bg_tasks.get(rest), paths.data_root()))
    else:
        app._system("usage: /tasks [list | kill <id> | tail <id>]")


def _register(ctx) -> None:
    ctx.command(
        ("/tasks",), _cmd_tasks,
        palette="Background tasks",
        help="Tool calls running on their own: list, kill, tail.",
        group="automation",
        order=40,
    )


PLUGIN = PluginManifest(id="tasks", register=_register)
