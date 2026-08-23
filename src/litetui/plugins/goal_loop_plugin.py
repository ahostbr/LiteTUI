"""First-party command surfaces for evidence goals and fixed loops."""
from litetui.goal_loop import GoalRuntime, goal_command, loop_command
from litetui.plugins import PluginManifest


def _goal(app, name: str, arg: str) -> None:
    goal_command(app, arg)


def _loop(app, name: str, arg: str) -> None:
    loop_command(app, arg)


def _register(ctx) -> None:
    runtime = GoalRuntime(ctx.app)
    ctx.app.goal_runtime = runtime
    ctx.command(
        ("/goal",),
        _goal,
        palette="Goal",
        help="Keep working until transcript evidence satisfies an objective.",
        group="automation",
        order=40,
    )
    ctx.command(
        ("/loop",),
        _loop,
        palette="Loop",
        help="Run a prompt repeatedly on a fixed cadence.",
        group="automation",
        order=50,
    )
    ctx.turn_finalizer(runtime.on_plain_answer)


PLUGIN = PluginManifest(id="goal-loop", register=_register)
