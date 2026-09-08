"""T526 probe: drive `litetui --rpc` by hand exactly as LiteTuiAdapter does, but with
stderr captured to a file, and print every stdout frame with a timestamp.

Usage: python scripts/t526_rpc_probe.py [seconds]  (run with the .venv python)
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERR = ROOT / "logs" / "t526_rpc_probe.stderr.txt"
WAIT = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0

print("interpreter:", sys.executable)
ERR.parent.mkdir(exist_ok=True)
err_f = open(ERR, "wb")
import os
env = dict(os.environ)
if os.environ.get("T526_VIA_UV"):
    # exactly what LiteTuiAdapter.ts spawns (shell:true, uv run ... litetui --rpc --cwd)
    argv = ["uv", "run", "--project", str(ROOT), "--locked", "--no-sync", "litetui", "--rpc", "--cwd", "C:\\Projects\\LiteSuite"]
    env["LITEHARNESS_SPAWNED_BY"] = "litesuite-frontier"
    env.pop("NO_COLOR", None)
else:
    argv = [sys.executable, "-m", "litetui.cli", "--rpc", "--cwd", "C:/Projects/LiteSuite"]
print("argv:", argv)
proc = subprocess.Popen(
    argv, cwd="C:\\Projects\\LiteSuite", env=env,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_f, text=True,
    encoding="utf-8", errors="replace", bufsize=1,
)
t0 = time.time()
events: list[tuple[float, dict]] = []
done = threading.Event()


def reader() -> None:
    assert proc.stdout
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            ev = {"type": "__nonjson__", "raw": line[:200]}
        events.append((time.time() - t0, ev))
        t = ev.get("type")
        summary = {k: (str(v)[:80]) for k, v in ev.items() if k in ("type", "id", "ok", "error", "model", "status", "text", "delta", "reason")}
        print(f"{time.time() - t0:7.2f}s  {summary}", flush=True)
        if t == "turn_end" or (t == "response" and ev.get("ok") is False):
            done.set()
    done.set()


threading.Thread(target=reader, daemon=True).start()

# wait for ready
deadline = time.time() + 30
while time.time() < deadline and not any(e.get("type") == "ready" for _, e in events):
    if proc.poll() is not None:
        break
    time.sleep(0.1)
if not any(e.get("type") == "ready" for _, e in events):
    print(f"NO READY within 30 s (exit={proc.poll()})")
else:
    assert proc.stdin
    proc.stdin.write(json.dumps({"type": "prompt", "id": "p1", "message": "hello, reply in one short line"}) + "\n")
    proc.stdin.flush()
    print(f"{time.time() - t0:7.2f}s  -> prompt sent", flush=True)
    done.wait(WAIT)

print(f"--- after {time.time() - t0:.1f}s: {len(events)} events, exit={proc.poll()}")
proc.kill()
err_f.close()
data = ERR.read_bytes()
print(f"stderr bytes: {len(data)}  (file {ERR})")
tail = data[-1500:].decode("utf-8", "replace")
print("stderr tail:\n" + tail)
