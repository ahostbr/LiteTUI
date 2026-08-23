"""Local generation (LiteImage/LiteSound/LiteModeler) — registration for studio_tool.

Offered unconditionally: availability is RUNTIME state (is the app open?),
and the tool reports its own precondition in one honest sentence, which
beats silently not existing when the app is closed.
"""
import studio_tool
from plugins import PluginManifest
from tool_policy import STUDIO_POLICY


def _register(ctx) -> None:
    app = ctx.app
    # The seat's identity rides in so generate actions can suspend the very
    # model making the call — the GPU is a single pie, and the agent's own
    # brain is the biggest slice (measured 2026-08-21: a 2.4 GB summarizer
    # beside the 29 GB seat near-OOMed the box). model_id is read at CALL
    # time, so a /model switch is honored mid-session.
    ctx.tool(
        studio_tool.STUDIO_TOOL_SPEC,
        # backend rides in too: the suspend/resume verbs must go to the
        # ENGINE serving the seat — lms verbs against a llama-served model
        # manage nothing. Read at call time, same as model_id.
        lambda args: studio_tool.run(
            args, seat_model=app.model_id, backend=app.backend
        ),
        policy=STUDIO_POLICY,
    )


PLUGIN = PluginManifest(id="studio", register=_register)
