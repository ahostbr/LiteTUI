"""Local generation (LiteImage/LiteSound/LiteModeler) — registration for studio_tool.

Offered unconditionally: availability is RUNTIME state (is the app open?),
and the tool reports its own precondition in one honest sentence, which
beats silently not existing when the app is closed.
"""
import studio_tool
from plugins import PluginManifest


def _register(ctx) -> None:
    app = ctx.app
    # The seat's identity rides in so generate actions can suspend the very
    # model making the call — the GPU is a single pie, and the agent's own
    # brain is the biggest slice (measured 2026-08-21: a 2.4 GB summarizer
    # beside the 29 GB seat near-OOMed the box). model_id is read at CALL
    # time, so a /model switch is honored mid-session.
    ctx.tool(
        studio_tool.STUDIO_TOOL_SPEC,
        lambda args: studio_tool.run(args, seat_model=app.model_id),
    )


PLUGIN = PluginManifest(id="studio", register=_register)
