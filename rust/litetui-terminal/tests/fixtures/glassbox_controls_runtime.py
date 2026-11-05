"""Real private Glassbox listener under the controlled registered app and RPC loop."""

from __future__ import annotations

import json
import os
import runpy
import socket
import time
from pathlib import Path
from unittest.mock import patch

connect_owned = socket.socket.connect
shared = runpy.run_path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
report = {"results": [], "threads": [], "exit_joined": False}
clients = []
threads = []


async def serve(app):
    from litetui import gui_rpc, native_glassbox
    from litetui.plugins import PluginContext
    from litetui.plugins import glassbox_plugin as gb

    source = Path(os.environ["RUST_RUNTIME_CONTROLS_SOURCE"])
    assert (
        Path(native_glassbox.__file__).resolve()
        == source / "src/litetui/native_glassbox.py"
    )
    assert (
        Path(gb.__file__).resolve() == source / "src/litetui/plugins/glassbox_plugin.py"
    )
    report["source"] = str(native_glassbox.__file__)
    gb.register(PluginContext(app, app.plugins, gb.PLUGIN_ID))
    owned = gb.Server()
    busy = socket.socket()
    busy.bind((gb.HOST, 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    actual_dispatch, actual_exit = gui_rpc.dispatch, app.exit

    def dispatch(owner, command):
        result = actual_dispatch(owner, command)
        if command["type"].startswith(native_glassbox.PREFIX):
            report["results"].append(result)
            if result["kind"] == "glassbox_verified" and result["status"] == "partial":
                busy.close()
            if (
                result["kind"] == "glassbox_applied"
                and result["action"] == "start"
                and result["status"] == "applied"
            ):
                listener = owned._httpd
                assert listener.server_address == (gb.HOST, port)
                connection = socket.socket()
                connection.settimeout(1)
                connect_owned(connection, listener.server_address)
                clients.append(connection)
                connection.sendall(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
                deadline = time.monotonic() + 2
                while not listener.subscribers:
                    assert time.monotonic() < deadline
                    time.sleep(0.005)
                threads.extend([owned._thread, *listener.clients])
                app.plugins.emit(
                    {
                        "channel": "output",
                        "intensity": 1.0,
                        "label": "private fixture event",
                    }
                )
                assert b"private fixture event" in gb._RECORDER.path.read_bytes()
            if result["kind"] == "glassbox_verified" and result["action"] == "stop":
                assert owned._httpd is None and not gb._ENABLED
                assert all(not thread.is_alive() for thread in threads)
        return result

    def exit_app(*args, **kwargs):
        assert owned._httpd is None and not gb._ENABLED
        assert all(not thread.is_alive() for thread in threads)
        report["exit_joined"] = True
        return actual_exit(*args, **kwargs)

    try:
        with (
            patch.object(gb, "SERVER", owned),
            patch.object(gb, "DEFAULT_PORT", port),
            patch.object(gui_rpc, "dispatch", dispatch),
            patch.object(app, "exit", exit_app),
        ):
            await shared["serve"](app)
    finally:
        report["threads"] = [{"name": t.name, "alive": t.is_alive()} for t in threads]
        report["recording"] = gb._ENABLED
        report["record_path"] = str(gb._RECORDER.path)
        report["file_preserved"] = gb._RECORDER.path.read_bytes().decode("utf-8")
        owned.stop(owner=app)
        busy.close()
        for connection in clients:
            connection.close()
        (root / "glassbox-controls.json").write_text(
            json.dumps(report), encoding="utf-8"
        )


shared["history"].serve = serve

if __name__ == "__main__":
    shared["history"].main()
