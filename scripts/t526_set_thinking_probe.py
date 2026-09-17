"""T526: what does `litetui --rpc` answer to the set_thinking frames LiteTuiAdapter can send?
Run with the .venv python. Prints every frame with a timestamp; exits after the last command's
response or 20 s.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

proc = subprocess.Popen(
    [sys.executable, "-m", "litetui.cli", "--rpc", "--cwd", "C:/Projects/LiteSuite"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    encoding="utf-8", errors="replace", bufsize=1,
)
t0 = time.time()
seen: list[dict] = []


def reader() -> None:
    assert proc.stdout
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            ev = {"type": "__nonjson__", "raw": line[:120]}
        seen.append(ev)
        print(f"{time.time() - t0:6.2f}s  {json.dumps(ev)[:160]}", flush=True)


threading.Thread(target=reader, daemon=True).start()
while time.time() - t0 < 30 and not any(e.get("type") == "ready" for e in seen):
    time.sleep(0.1)

assert proc.stdin
for i, level in enumerate([None, "off", "low", "medium"]):
    cmd = {"type": "set_thinking", "id": f"st{i}", "level": level}
    proc.stdin.write(json.dumps(cmd) + "\n"); proc.stdin.flush()
    print(f"{time.time() - t0:6.2f}s  -> {json.dumps(cmd)}", flush=True)
    deadline = time.time() + 5
    while time.time() < deadline and not any(e.get("id") == f"st{i}" for e in seen):
        time.sleep(0.05)
    if not any(e.get("id") == f"st{i}" for e in seen):
        print(f"        NO RESPONSE to st{i} within 5 s", flush=True)

proc.kill()
