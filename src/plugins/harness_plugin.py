"""LiteHarness fleet — the harness tool + the inbox monitor's start.

Seat CONSTRUCTION stays host-owned in __init__ (a core ordering guarantee —
monitors must never race the seat's existence); this plugin owns the tool
surface and WHEN the inbox subsystem starts.
"""
import harness as harness_mod
from plugins import PluginManifest
from tool_policy import HARNESS_POLICY


def _register(ctx) -> None:
    app = ctx.app
    # Only offered once the seat is actually registered. Advertising fleet
    # verbs to an agent with no return address produces confident sends
    # that go nowhere.
    ctx.tool(
        harness_mod.HARNESS_TOOL_SPEC,
        lambda args: harness_mod.run(app.seat, args),
        gate=lambda: app.seat.registered,
        policy=HARNESS_POLICY,
    )


def _activate(app) -> None:
    app._inbox_monitor()


PLUGIN = PluginManifest(id="harness", register=_register, activate=_activate)
