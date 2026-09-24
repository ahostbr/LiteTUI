"""Opt-in actual JSONL RPC process: context, queue, stop, native tools/questions."""
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


def main():
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1; no model call made")
    from litetui import settings
    with tempfile.TemporaryDirectory(prefix="litetui-claude-rpc-") as directory:
        root = Path(directory)
        settings.save(settings.Settings(backend="claude", backend_chosen=True,
            default_model="default", tools_enabled=True, skills_enabled=False,
            mcp_enabled=False, plugins_disabled=["scheduler", "skills", "harness", "glassbox"],
            tool_policy_profile="strict"), root=root)
        process = subprocess.Popen([sys.executable, "-m", "litetui.cli", "--rpc", "--backend", "claude"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, LITETUI_DATA_ROOT=str(root), LITETUI_NO_HARNESS="1"),
            cwd=root, text=True, encoding="utf-8")
        lines = queue.Queue()
        events = []
        stderr = []
        def read():
            for line in process.stdout:
                try:
                    lines.put(json.loads(line))
                except ValueError:
                    pass
            lines.put({"type": "process_exit"})
        def read_err():
            stderr.extend(process.stderr)
        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=read_err, daemon=True).start()
        def send(type, **values):
            process.stdin.write(json.dumps({"type": type, **values}) + "\n")
            process.stdin.flush()
        def until(type, timeout=60):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                event = lines.get(timeout=max(.01, deadline-time.monotonic()))
                events.append(event)
                if event.get("type") == "process_exit":
                    raise AssertionError("RPC exited: " + "".join(stderr)[-2500:])
                if event.get("type") == "submit_refused":
                    raise AssertionError(event)
                if event.get("type") == type:
                    return event
            raise AssertionError(f"No {type}")
        try:
            until("ready")
            send("prompt", id="first", message="Remember ORCHID-631. Reply only READY.")
            until("turn_start")
            # Busy queue must persist and deliver as a second native turn.
            send("prompt", id="queued", message="What is the remembered word? Reply only with it.")
            assert until("turn_end").get("stopReason") == "stop"
            assert until("turn_end").get("stopReason") == "stop"
            assert any("ORCHID-631" in str(e.get("text", "")) for e in events)
            target = root / "approved.txt"
            send("prompt", id="write", message=f"Use Write to create {target} containing APPROVED. Do not use another tool.")
            approval = until("tool_approval_requested")
            send("approve", id="allow", approval_id=approval["id"], allow=True)
            assert until("turn_end").get("stopReason") == "stop"
            assert target.read_text().strip() == "APPROVED"
            denied = root / "denied.txt"
            send("prompt", id="deny-write", message=f"Use Write to create {denied} containing DENIED. If denied, stop.")
            approval = until("tool_approval_requested")
            send("approve", id="deny", approval_id=approval["id"], allow=False)
            until("turn_end")
            assert not denied.exists()
            send("prompt", id="question", message="Use AskUserQuestion to ask which test color I prefer, Red or Blue. After I answer reply only with my chosen color.")
            ask = until("user_input_requested")
            selected_label = ask["questions"][0]["options"][1]["title"]
            answer_start = len(events)
            send("answer", id="answer", ask_id=ask["id"], action="submit", answers=[{"selected": [1]}])
            assert until("turn_end").get("stopReason") == "stop"
            answer_text = "".join(e.get("text", "") for e in events[answer_start:] if e.get("type") == "text_delta")
            assert selected_label.lower() in answer_text.lower(), answer_text
            send("prompt", id="stop-question", message="Use AskUserQuestion to ask which test shape I prefer, Circle or Square.")
            until("user_input_requested")
            send("abort", id="stop")
            until("turn_end")
            after_stop = len(events)
            send("prompt", id="after-stop", message="Reply only AFTER-STOP.")
            assert until("turn_end").get("stopReason") == "stop"
            assert "AFTER-STOP" in "".join(e.get("text", "") for e in events[after_stop:] if e.get("type") == "text_delta")
            # MCP host service through the guarded dispatcher, never native re-execution.
            before = len(events)
            send("prompt", id="host", message="Call mcp__litetui__chrome with action status exactly once, then reply HOST-DONE. Do not use any other tool.")
            assert until("turn_end").get("stopReason") == "stop"
            host_calls = [e for e in events[before:] if e.get("type") == "tool_call" and e.get("name") == "chrome"]
            assert len(host_calls) == 1, host_calls
            send("shutdown", id="shutdown")
            process.wait(timeout=20)
            assert process.returncode == 0
            out = Path(__file__).resolve().parents[1] / "artifacts" / "claude-rpc-smoke.json"
            out.write_text(json.dumps({"context": True, "durable_queue": True,
                "native_approved_write": True, "native_denied_write": True,
                "question_answer": True, "stop_pending_question_then_next_turn": True,
                "process_exit": process.returncode,
                "events": events}, indent=2), encoding="utf-8")
            print(f"PASS {out}")
        finally:
            if process.poll() is None:
                send("shutdown", id="cleanup")
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
            if process.stdin:
                process.stdin.close()


if __name__ == "__main__":
    main()
