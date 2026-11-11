"""Controlled stdio child: protocol only, no GUI/provider/device/network calls."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

root = Path(os.environ["LITETUI_DATA_ROOT"])
token = os.environ["LITETUI_SIDECAR_TOKEN"]
lock = threading.Lock()
settings = None


def send(ident, command, payload):
    with lock:
        print(json.dumps({"version": 1, "id": ident, "token": token, "command": command, "payload": payload}), flush=True)


def watch():
    seen = set()
    while True:
        for name in ("settings", "job"):
            path = root / ("sidecar-propose-" + name + ".json")
            if name in seen or not path.exists() or settings is None:
                continue
            draft = json.loads(path.read_text(encoding="utf8"))
            if name == "settings":
                draft = {"conversation_id": settings["conversation_id"], "expected_revisions": settings["revisions"],
                    "changes": [{"key": k, "value": v, "scope": settings["fields"][k]["scope"]} for k, v in draft.items()]}
            seen.add(name)
            send(700 if name == "settings" else 701, "settings_patch" if name == "settings" else "job_create", draft)
            (root / ("sidecar-sent-" + name)).touch()
        time.sleep(0.01)


threading.Thread(target=watch, daemon=True).start()
for line in sys.stdin:
    frame = json.loads(line)
    assert frame["token"] == token and frame["version"] == 1
    name, payload = frame["command"], frame["payload"]
    if name == "event_reply":
        with (root / "sidecar-final-replies.jsonl").open("a", encoding="utf8") as output:
            output.write(json.dumps({"id": frame["id"], "payload": payload}) + "\n")
        if isinstance(payload, dict) and "snapshot" in payload:
            settings = payload["snapshot"]
        continue
    if name == "hello":
        result = {"version": 1, "ready": True}
    elif name == "settings_snapshot":
        settings = payload
        (root / "sidecar-delivered.json").write_text(json.dumps(payload), encoding="utf8")
        result = {name: True}
    elif name == "jobs_snapshot":
        result = {name: True}
    elif name == "open":
        result = {"opened": payload["view"]}
    else:
        raise AssertionError(name)
    send(frame["id"], "reply", result)
