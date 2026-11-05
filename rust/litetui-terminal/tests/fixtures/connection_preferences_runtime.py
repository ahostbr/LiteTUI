"""Run registered source dispatch against disposable settings, denying every network and process boundary."""

import json
import os
import runpy
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
attempts = []
saved = []


def forbidden(*_args, **_kwargs):
    attempts.append("connection, model load, probe or process attempt")
    raise AssertionError(attempts[-1])


async def serve(app):
    import asyncio

    import httpx

    from litetui import gui_rpc, settings_runtime

    actual = gui_rpc.dispatch

    def dispatch(owner, command):
        if not command["type"].startswith("gui.connection.native_"):
            return actual(owner, command)
        with ExitStack() as guard:
            for target, name in (
                (httpx.Client, "send"),
                (httpx.AsyncClient, "send"),
                (socket.socket, "connect"),
                (socket.socket, "connect_ex"),
                (socket, "create_connection"),
                (subprocess.Popen, "__init__"),
                (os, "system"),
                (asyncio, "create_subprocess_exec"),
                (asyncio, "create_subprocess_shell"),
                (owner, "_apply_context_length"),
                (owner, "apply_backend_change"),
                (owner, "connect"),
                (owner.backend, "load"),
                (owner.backend, "unload"),
                (owner.backend, "list_models"),
                (owner.backend, "ensure_running"),
                (settings_runtime, "prepare_reconnect"),
            ):
                guard.enter_context(patch.object(target, name, forbidden))
            result = actual(owner, command)
        if command["type"].endswith("readback"):
            document = json.loads((owner.convo_dir / "settings.json").read_bytes())
            execution = document.get("execution", document)
            saved.append(
                {
                    "saved": {
                        key: execution.get(key)
                        for key in ("lm_host", "pin_default_model", "custom_api_key_env")
                    },
                    "effective": {
                        key: getattr(owner.settings, key)
                        for key in ("lm_host", "pin_default_model", "custom_api_key_env")
                    },
                }
            )
        return result

    try:
        with patch.object(gui_rpc, "dispatch", dispatch):
            await shared["serve"](app)
    finally:
        (root / "connection-guards.json").write_text(json.dumps(attempts), encoding="utf-8")
        (root / "connection-saved.json").write_text(json.dumps(saved), encoding="utf-8")


shared["history"].serve = serve
if __name__ == "__main__":
    shared["history"].main()
