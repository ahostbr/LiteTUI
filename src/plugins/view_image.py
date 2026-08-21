"""Stage an image for the model to SEE.

The model cannot see an image through a tool result: a tool result is a
role:"tool" message whose content is a STRING; images are only visible as an
image_url block on a role:"user" message. So this tool does not return the
picture — it stages it (app._pending_tool_images, agent-loop infrastructure),
and the tool loop injects a user turn carrying the image through the same
door the paste path uses. Returning base64 here would burn a megabyte of
context to show the model nothing.
"""
from plugins import PluginManifest

VIEW_IMAGE_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "view_image",
        "description": (
            "Look at an image file on disk. Give an absolute path. The image is "
            "attached to the conversation and you will see it in the next message "
            "-- the result of this call is only a confirmation, not the picture."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path to a png/jpg/gif/webp/bmp file",
                }
            },
            "required": ["path"],
        },
    },
}


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
    )


PLUGIN = PluginManifest(id="view-image", register=_register)
