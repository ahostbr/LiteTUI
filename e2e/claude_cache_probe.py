"""T911 opt-in live check: the cache warms across turns, and a cold resume warns first.

Phase 1: three Claude turns in one headless LiteTUI. cache_read_input_tokens must
grow turn over turn (each turn re-reads the one before it).
Phase 2: the saved segment's last cache use is backdated two hours, as if the
user came back after lunch. A fresh LiteTUI resumes the conversation: the first
prompt must produce `cache_warning` and NO turn_start (nothing sent); the second
prompt within the window is the go-ahead and runs.

Plan usage only (about 4 short turns); no local model.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


class Rpc:
    def __init__(self, root, convo=None):
        cmd = [sys.executable, "-m", "litetui.cli", "--rpc", "--backend", "claude"]
        if convo:
            cmd += ["--convo", convo]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  env=dict(os.environ, LITETUI_DATA_ROOT=str(root), LITETUI_NO_HARNESS="1"),
                                  cwd=root, text=True, encoding="utf-8")
        self.q, self.events, self.err = queue.Queue(), [], []
        threading.Thread(target=self._out, daemon=True).start()
        threading.Thread(target=lambda: self.err.extend(self.p.stderr), daemon=True).start()

    def _out(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put({"type": "process_exit"})

    def send(self, type, **values):
        self.p.stdin.write(json.dumps({"type": type, **values}) + "\n")
        self.p.stdin.flush()

    def until(self, *types, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                e = self.q.get(timeout=max(.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            self.events.append(e)
            if e.get("type") == "process_exit":
                raise AssertionError("RPC exited: " + "".join(self.err)[-2500:])
            if e.get("type") in types:
                return e
        return None

    def close(self):
        if self.p.poll() is None:
            self.send("shutdown", id="bye")
            try:
                self.p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.p.kill()


def turn(rpc, id, message):
    start = len(rpc.events)
    rpc.send("prompt", id=id, message=message)
    end = rpc.until("turn_end")
    assert end and end.get("stopReason") == "stop", end
    usages = [e["data"] for e in rpc.events[start:]
              if e.get("type") == "native_usage" and e.get("data", {}).get("source") == "message"]
    return {"id": id, "requests": len(usages),
            "cache_read": [u.get("cache_read_tokens") for u in usages],
            "cache_write": [u.get("cache_creation_tokens") for u in usages],
            "uncached": [u.get("input_tokens") for u in usages]}


def main():
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1; no model call made")
    from litetui import settings
    out = {}
    with tempfile.TemporaryDirectory(prefix="litetui-claude-cache-") as directory:
        root = Path(directory)
        settings.save(settings.Settings(backend="claude", backend_chosen=True, default_model="default",
            tools_enabled=False, skills_enabled=False, mcp_enabled=False,
            plugins_disabled=["scheduler", "skills", "harness", "glassbox"]), root=root)
        rpc = Rpc(root)
        try:
            assert rpc.until("ready", timeout=60)
            out["turns"] = [turn(rpc, f"t{i}", m) for i, m in enumerate([
                "Remember the word CEDAR-914. Reply only OK.",
                "What word did I ask you to remember? Reply only with it.",
                "Reply only with the word reversed, letter by letter.",
            ], 1)]
        finally:
            rpc.close()
        reads = [max(t["cache_read"] or [0]) for t in out["turns"]]
        out["cache_read_grows"] = all(b > a for a, b in zip(reads, reads[1:]))

        ledger_file = next(root.rglob("claude_ledger.json"))
        convo = ledger_file.parent.name
        data = json.loads(ledger_file.read_text(encoding="utf-8"))
        seg = data["segments"][data["selected_segment"]]
        out["saved_last_use_s_ago"] = round(time.time() - seg["cache_used_at"], 1)
        seg["cache_used_at"] -= 7200  # as if the user came back two hours later
        ledger_file.write_text(json.dumps(data), encoding="utf-8")

        rpc = Rpc(root, convo=convo)
        try:
            assert rpc.until("ready", timeout=60)
            start = len(rpc.events)
            rpc.send("prompt", id="cold", message="Reply only STILL-HERE.")
            warning = rpc.until("cache_warning", "turn_start", timeout=30)
            out["cold_first_event"] = warning.get("type") if warning else None
            out["cold_warning"] = warning
            late = rpc.until("turn_start", timeout=8)
            out["sent_before_ack"] = late is not None or any(
                e.get("type") == "turn_start" for e in rpc.events[start:])
            out["after_ack"] = turn(rpc, "cold-ack", "Reply only STILL-HERE.")
        finally:
            rpc.close()
    ok = (out["cache_read_grows"] and out["cold_first_event"] == "cache_warning"
          and not out["sent_before_ack"] and out["after_ack"]["requests"] >= 1)
    art = Path(__file__).resolve().parents[1] / "artifacts" / "claude-cache-probe.json"
    art.write_text(json.dumps({"pass": ok, **out}, indent=2), encoding="utf-8")
    print(("PASS " if ok else "FAIL ") + str(art))
    print(json.dumps({k: v for k, v in out.items() if k != "cold_warning"}, indent=1))
    if out.get("cold_warning"):
        print("warning:", out["cold_warning"].get("reason"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
