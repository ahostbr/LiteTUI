"""`monitor` — the tool that lets a seat watch a command or a file and be woken.

Not the `/monitor` slash command (plugins/monitor_plugin.py), which is a human
browser-target sweep. This one is a tool the MODEL calls; the machinery is in
litetui/agent_monitor.py and is Textual-free. This file only supplies the app's
side of it: how an event reaches the seat, and when a watch is no longer wanted.

An event is delivered with `app._deliver_inbox`, the funnel a finished background
task already uses (app._run_background): held while a turn is running, a wake
when the seat is idle. Monitor threads are not the UI thread, so the call crosses
with `call_from_thread`.
"""

from litetui import agent_monitor, tool_schemas
from litetui.plugins import PluginManifest


def _register(ctx) -> None:
    app = ctx.app

    def deliver(text: str) -> None:
        def on_loop() -> None:
            app._deliver_inbox({"from": "monitor", "priority": "normal", "body": text})
        app.call_from_thread(on_loop)

    service = agent_monitor.MonitorService(
        deliver=deliver,
        convo_id=lambda: getattr(app, "convo_id", "") or "",
        running=lambda: not getattr(app, "_gui_quitting", False) and app.is_running,
        agent_id=getattr(getattr(app, "seat", None), "agent_id", None),
    )
    app.agent_monitors = service
    ctx.tool(tool_schemas.load("monitor"), service.run, policy=agent_monitor.POLICY)


PLUGIN = PluginManifest(id="agent_monitor", register=_register)
