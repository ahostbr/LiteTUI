"""Audio perception (Qwen2-Audio via standalone llama-server) — listen_tool.

Offered unconditionally like studio: availability is RUNTIME state (is the
server binary there, are the model files present), and action=status reports
its own precondition in one honest sentence, which beats silently not
existing when something is missing.
"""
from litetui import listen_tool
from litetui.plugins import PluginManifest
from litetui.tool_policy import LISTEN_POLICY


def _register(ctx) -> None:
    app = ctx.app
    # The seat's identity rides in so a real listen can suspend the very model
    # making the call — Qwen2-Audio and the agent's own brain cannot share
    # VRAM (measured 2026-08-30: they evict each other). model_id is read at
    # CALL time, so a /model switch is honored mid-session. The backend handle
    # routes the suspend/resume verbs to the ENGINE that owns the seat — lms
    # verbs against a llama-served model manage nothing (same finding as
    # studio's registration).
    ctx.tool(
        listen_tool.LISTEN_TOOL_SPEC,
        lambda args: listen_tool.run(
            args, seat_model=app.model_id, backend=app.backend
        ),
        policy=LISTEN_POLICY,
    )


PLUGIN = PluginManifest(id="listen", register=_register)
