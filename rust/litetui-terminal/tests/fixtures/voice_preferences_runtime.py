"""Run registered source dispatch against disposable settings, denying every audio boundary."""

import json
import os
import runpy
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
attempts = []


def forbidden(*_args, **_kwargs):
    attempts.append("voice hardware, service or installation attempt")
    raise AssertionError(attempts[-1])


async def serve(app):
    from litetui import (
        gui_rpc,
        listen_tool,
        litesuite_tts,
        stt_backend,
        voice_backend,
        voice_install,
    )

    actual = gui_rpc.dispatch

    def dispatch(owner, command):
        if not command["type"].startswith("gui.voice.native_"):
            return actual(owner, command)
        with ExitStack() as guard:
            for module, names in (
                (
                    voice_backend,
                    ("stop", "speak", "list_sapi_voices", "available_engines"),
                ),
                (
                    stt_backend,
                    (
                        "available",
                        "missing",
                        "list_mics",
                        "record_start",
                        "record_stop",
                        "transcribe",
                    ),
                ),
                (litesuite_tts, ("_available", "speak")),
                (listen_tool, ("run", "_status", "_start_server")),
                (voice_install, ("edge_available", "install_command", "install_edge")),
            ):
                for name in names:
                    guard.enter_context(patch.object(module, name, forbidden))
            return actual(owner, command)

    try:
        with patch.object(gui_rpc, "dispatch", dispatch):
            await shared["serve"](app)
    finally:
        (root / "voice-guards.json").write_text(json.dumps(attempts), encoding="utf-8")


shared["history"].serve = serve
if __name__ == "__main__":
    shared["history"].main()
