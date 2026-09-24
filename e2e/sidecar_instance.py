"""One LiteTUI instance for the two-instance sidecar proof (driven by
e2e/sidecar_two_instance_live.py; not run on its own).

A real LiteTUI app (headless Textual run_test) on the Claude backend, with its
own conversation, that opens its own real litetui-sidecar window. The driver
talks to it only through two files in its work directory:

    cmd.json    {"n": <int>, "cmd": "state" | "turn" | "quit"}
    state.json  {"n": <same int>, ...what THIS TUI holds...}

Environment (set by the driver): LITETUI_DATA_ROOT (shared by both instances,
exactly like two real seats on one machine), LITETUI_SIDECAR_EXE,
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS (this window's own debug port).
"""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["LITETUI_NO_HARNESS"] = "1"

from litetui import app as app_mod  # noqa: E402

FIELDS = ("autocompact_at_percent", "thinking_level", "show_stop_time", "backend")


async def settle(app, pilot, groups=("init", "chat")):
    for _ in range(1500):
        await pilot.pause(0.1)
        if not any(w.is_running and w.group in groups for w in app.workers):
            return


def state(app, n):
    from litetui import settings_runtime

    snap = settings_runtime.service_for(app).snapshot(app.convo_dir.name)
    return {
        "n": n, "pid": os.getpid(), "convo": app.convo_dir.name,
        "backend": app.backend.name, "model": app.model_id,
        "in_memory": {k: getattr(app.settings, k) for k in FIELDS} | {"_thinking_level": app._thinking_level},
        "disk_saved": {k: getattr(snap.saved, k) for k in FIELDS},
        "disk_effective": {k: getattr(snap.effective, k) for k in FIELDS},
        "revisions": snap.revisions,
        "preapproved": getattr(app, "_claude_cache_preapproved", None),
        "notices": [str(n) for n in getattr(app, "_notices_for_test", [])][-8:],
    }


async def main(work: Path):
    app = app_mod.LiteTUI()
    notices = app._notices_for_test = []
    original = app.system_message
    app.system_message = lambda text, *a, **k: (notices.append(text), original(text, *a, **k))[1]
    async with app.run_test(size=(140, 50)) as pilot:
        await settle(app, pilot)
        app._materialise_convo()
        app._handle_command("/sidecar settings")
        for _ in range(40):
            await pilot.pause(0.25)
        # What the sidecar command said, for a failed launch to be diagnosable.
        (work / "boot.json").write_text(json.dumps(
            {"notices": [str(n) for n in notices], "sidecar_enabled": app.settings.sidecar_enabled,
             "exe": os.environ.get("LITETUI_SIDECAR_EXE")}, default=str), encoding="utf-8")
        seen = -1
        while True:
            await pilot.pause(0.2)
            try:
                cmd = json.loads((work / "cmd.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if cmd["n"] == seen:
                continue
            seen = cmd["n"]
            if cmd["cmd"] == "quit":
                break
            if cmd["cmd"] == "turn":
                app._submit_text(cmd.get("text", "Reply only OK."), False)
                await settle(app, pilot)
            (work / "state.tmp").write_text(json.dumps(state(app, seen), default=str), encoding="utf-8")
            os.replace(work / "state.tmp", work / "state.json")
        owner = getattr(app, "_sidecar_preview", None)
        if owner is not None:
            owner.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
