"""T558-A manual verification: a REAL `litetui --rpc` child answers a question.

Not a test, and deliberately not in tests/: it needs a live model that CHOOSES to
call `ask_user_question`, which is non-deterministic and cannot be a gate. The
suite covers the wire logic and the routing; this covers the one thing the suite
structurally cannot — that a model, on its own, reaches the tool and gets an
answer back through the rpc verbs.

    python scripts/t558a_ask_over_rpc_manual.py [--model SLUG] [--timeout SECS]

It prints every JSONL line the child emits, marked, so the transcript can go into
a commit body verbatim.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROMPT = (
    "Before you do anything else, call the ask_user_question tool once to ask me "
    "which of two approaches I want: 'A' or 'B'. Do not answer it yourself and do "
    "not write the question as text — use the tool."
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="model slug (default: whatever is loaded)")
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    # NOT `python -m litetui`: the package has no __main__.py, so that spelling exits
    # immediately with "cannot be directly executed" and the child emits NOTHING. My
    # first run did exactly that and the script blamed the MODEL for not calling the
    # tool — a diagnostic naming a plausible cause it had no evidence for. The entry
    # point is cli:main (pyproject [project.scripts]).
    cmd = [sys.executable, "-c", "from litetui.cli import main; main()", "--rpc", "--cwd", str(ROOT)]
    if args.model:
        cmd += ["--model", args.model]

    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("NO_COLOR", None)

    print(f"[spawn] {' '.join(cmd)}", flush=True)
    child = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # textual paints here; only fd 1 carries JSONL
        env=env,
        text=True,
        bufsize=1,
        cwd=str(ROOT),
    )

    state = {"asked": False, "answered": False, "done": False, "ask_id": None}
    lines: list[dict] = []

    def send(obj: dict) -> None:
        print(f"[-> child] {json.dumps(obj)}", flush=True)
        child.stdin.write(json.dumps(obj) + "\n")
        child.stdin.flush()

    def pump() -> None:
        for raw in child.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[child non-json] {raw[:200]}", flush=True)
                continue
            lines.append(evt)
            kind = evt.get("type")
            print(f"[child] {json.dumps(evt)[:400]}", flush=True)

            if kind == "ready":
                send({"type": "prompt", "id": "p1", "message": PROMPT})

            elif kind == "user_input_requested":
                # THE THING BEING VERIFIED: the question reached the wire at all.
                state["asked"] = True
                state["ask_id"] = evt.get("id")
                send({
                    "type": "answer",
                    "id": evt.get("id"),
                    "action": "submit",
                    "answers": [{"selected": [0], "note": "answered by the manual script"}],
                })

            elif kind == "response" and state["asked"] and evt.get("ok") and \
                    isinstance(evt.get("result"), dict) and evt["result"].get("answered"):
                state["answered"] = True

            elif kind == "turn_end":
                state["done"] = True
                return

    t = threading.Thread(target=pump, daemon=True)
    t.start()

    deadline = time.time() + args.timeout
    while time.time() < deadline and not state["done"]:
        time.sleep(0.25)

    try:
        send({"type": "shutdown"})
    except Exception:
        pass
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()

    print("\n==== RESULT ====", flush=True)
    print(f"question reached the wire : {state['asked']}", flush=True)
    print(f"answer accepted by child  : {state['answered']}", flush=True)
    print(f"turn completed            : {state['done']}", flush=True)
    print(f"events seen               : {len(lines)}", flush=True)

    # The tool's own return text, if the child echoed it back as a tool result.
    for evt in lines:
        text = json.dumps(evt)
        if "ask_user_question" in text and ("SUBMITTED" in text or "UNANSWERED" in text):
            print(f"tool result               : {text[:300]}", flush=True)
            break

    if not lines:
        # DISTINCT from "the model did not ask": nothing came back at all, so the
        # child never started or its stdout never reached us. Blaming the model here
        # is a diagnosis with no evidence behind it.
        print(
            "\nThe child emitted NOTHING - it never started, or its stdout is not "
            "reaching this script. Re-run the spawn command by hand WITHOUT "
            "stderr=DEVNULL to see why. This says nothing about the model or the wire.",
            flush=True,
        )
        return 3

    if not state["asked"]:
        print(
            "\nThe model never called ask_user_question. That is a MODEL outcome, not a "
            "wire failure — re-run, or use a model that follows the instruction. It does "
            "NOT show the rpc path is broken; tests/test_ask_over_rpc.py covers that.",
            flush=True,
        )
        return 2
    return 0 if state["answered"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
