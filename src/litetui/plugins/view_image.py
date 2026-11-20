"""Show the model an image: stage pixels for a model that can see, or give a
model that cannot a fast TEXT report of the picture instead.

VISION MODELS. The model cannot see an image through a tool result: a tool
result is a role:"tool" message whose content is a STRING; images are only
visible as an image_url block on a role:"user" message. So this tool does not
return the picture — it stages it (app._pending_tool_images, agent-loop
infrastructure), and the tool loop injects a user turn carrying the image
through the same door the paste path uses. Returning base64 here would burn a
megabyte of context to show the model nothing. That path is app._tool_view_image
and is untouched.

TEXT-ONLY MODELS (model_type "llm", T1319). Staging pixels for them 400s or is
ignored, so they get litetui.image_describe's measurement report as the tool
result: colours by region, the foreground silhouette's width per row band,
bright and dark regions, and with `compare_to` a reference-vs-render diff. The
decision is made per CALL, from model_type read live, so a /model switch changes
the behaviour on the next call without changing the tool array.
"""
from litetui.plugins import PluginManifest
from litetui import tool_schemas
from litetui.tool_policy import READ_POLICY

VIEW_IMAGE_TOOL_SPEC = tool_schemas.load("view_image")


def _register(ctx) -> None:
    app = ctx.app

    def _run(args: dict) -> str:
        # Only a model KNOWN to be text-only takes the report path. Unknown
        # (None) keeps today's behaviour: the staging path reports its own
        # precondition. `compare_to` belongs to the report path alone.
        if app.model_type == "llm":
            from litetui import image_describe
            out = image_describe.describe_tool(args)
            # A backend that knows WHY its model is text-only right now (Basalt started with
            # image input off although the pack has a projector) says so, and what to do,
            # in place of "this model cannot see images".
            why = getattr(getattr(app, "backend", None), "image_input_off_note", None)
            note = why(getattr(app, "settings", None)) if callable(why) else None
            if note and out.startswith(image_describe.NOTE):
                out = note + out[len(image_describe.NOTE):]
            return out
        return app._tool_view_image(args)

    # Always offered (T1319): there is a useful answer for every model, so the
    # tool array no longer depends on model_type, which also keeps the prompt
    # prefix identical across a /model switch (T1306-D).
    ctx.tool(VIEW_IMAGE_TOOL_SPEC, _run, policy=READ_POLICY)


PLUGIN = PluginManifest(id="view-image", register=_register)
