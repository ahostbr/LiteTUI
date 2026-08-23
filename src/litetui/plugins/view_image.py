"""Stage an image for the model to SEE.

The model cannot see an image through a tool result: a tool result is a
role:"tool" message whose content is a STRING; images are only visible as an
image_url block on a role:"user" message. So this tool does not return the
picture — it stages it (app._pending_tool_images, agent-loop infrastructure),
and the tool loop injects a user turn carrying the image through the same
door the paste path uses. Returning base64 here would burn a megabyte of
context to show the model nothing.
"""
from litetui.plugins import PluginManifest
from litetui import tool_schemas
from litetui.tool_policy import READ_POLICY

VIEW_IMAGE_TOOL_SPEC = tool_schemas.load("view_image")


def _register(ctx) -> None:
    app = ctx.app
    # Offered unless we KNOW the model cannot see (type "llm"). Unknown stays
    # offered: the tool reports the precondition itself, which is more useful
    # than the tool silently not existing. The gate reads model_type LIVE at
    # every spec assembly, so a /model switch flips the offer next turn.
    ctx.tool(
        VIEW_IMAGE_TOOL_SPEC,
        lambda args: app._tool_view_image(args),
        gate=lambda: app.model_type != "llm",
        policy=READ_POLICY,
    )


PLUGIN = PluginManifest(id="view-image", register=_register)
