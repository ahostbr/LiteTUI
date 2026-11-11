"""Private Mark handoff: no desktop, screen, mouse, microphone or provider operations."""

import base64
import io
import json
import os
import runpy
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image

shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
real_popen = subprocess.Popen


async def serve(app):
    from litetui import native_mark
    from litetui.plugins import PluginContext, mark_plugin

    mark_plugin.PLUGIN.register(PluginContext(app, app.plugins, "mark"))

    image = io.BytesIO()
    Image.new("RGB", (80, 40), (30, 180, 220)).save(image, format="PNG")
    pixels = base64.b64encode(image.getvalue()).decode("ascii")
    script = root / "private_marker.py"
    script.write_text(
        "import base64,json,pathlib,sys\np=pathlib.Path(sys.argv[1])\n"
        + f"(p/'mark.png').write_bytes(base64.b64decode({pixels!r}))\n"
        + "(p.parent.parent/'mark-started').touch()\n"
        + "v={'x':-1900,'y':25,'mon':1,'mon_x':20,'mon_y':25,'png':str(p/'mark.png'),'text':'Review this fixture Δ'}\n"
        + "(p/'mark.json').write_text(json.dumps(v),encoding='utf-8-sig')\n",
        encoding="utf-8",
    )
    marker = root / "private-marker.ps1"
    marker.write_text("Private test marker, never executed", encoding="utf-8")
    (root / "prior.png").write_bytes(image.getvalue())

    def command(capture):
        return [sys.executable, "-B", str(script), str(capture.directory)]

    def launch(argv, **kwargs):
        if argv != command(app._native_mark):
            raise AssertionError("Mark fixture refused an unexpected child")
        return real_popen(argv, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(native_mark.plugin, "MARK_SCRIPT", marker))
        stack.enter_context(patch.object(native_mark, "_launcher", lambda: sys.executable))
        stack.enter_context(patch.object(native_mark, "_command", command))
        stack.enter_context(patch.object(native_mark.plugin, "start_mark", side_effect=AssertionError("Legacy marker forbidden")))
        stack.enter_context(patch.object(subprocess, "Popen", launch))
        try:
            await shared["serve"](app)
        finally:
            from litetui import background_input as work

            bridge = work.bridge(app)
            entry = getattr(bridge, "composer_image", None)
            (root / "mark-after.json").write_text(json.dumps({
                "retired": not native_mark.blocking(app),
                "image": entry[0] if entry else None,
                "pending": len(app._pending_input),
            }), encoding="utf-8")


shared["history"].serve = serve
if __name__ == "__main__":
    shared["history"].main()
