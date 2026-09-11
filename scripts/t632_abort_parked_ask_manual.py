"""T632 manual verification: a REAL `litetui --rpc` child, parked in a real ask, is released by `abort`.

Not a test, and deliberately not in tests/: arm 1 needs a live model that CHOOSES
to call `ask_user_question`, which is non-deterministic and cannot be a gate.
tests/test_abort_releases_a_parked_ask.py covers the wire logic; this covers the
one thing the suite structurally cannot — the SUBPROCESS boundary and a real
turn actually ending.

    python scripts/t632_abort_parked_ask_manual.py --model SLUG [--timeout SECS]

🔴 IT WILL NOT LOAD A MODEL, BY CONSTRUCTION OF THE CHILD, NOT BY POLITENESS.
`_headless_model_decision` (app.py:4368) returns ok / substitute / refuse and
never loads: with `--model` naming a RESIDENT model it takes the `ok` branch.
Pass a slug that `lms ps` already lists, and compare `lms ps` before and after.

Two arms, both reported with timings:
  1. parked in an ask -> abort -> user_input_resolved, {stopped:true}, turn ends
  2. idle child       -> abort -> {stopped:false}, nothing cancelled
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


def spawn(model: str | None) -> subprocess.Popen:
    # NOT `python -m litetui` — no __main__.py; that spelling emits nothing and
    # the failure reads as the model's fault. Entry point is cli:main.
    cmd = [sys.executable, "-c", "from litetui.cli import main; main()",
           "--rpc", "--cwd", str(ROOT)]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("NO_COLOR", None)
    print(f"[spawn] {' '.join(cmd)}", flush=True)
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1, cwd=str(ROOT),
    )


def _closer(child: subprocess.Popen) -> None:
    try:
        child.stdin.write(json.dumps({"type": "shutdown"}) + "\n")
        child.stdin.flush()
    except Exception:
        pass
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()


def arm_parked(model: str | None, timeout: float) -> dict:
    """The card: a thread parked in an ask, released by abort."""
    child = spawn(model)
    st: dict = {
        "asked": False, "ask_id": None, "resolved": None, "abort_reply": None,
        "turn_end": False, "t_ask": None, "t_abort": None,
        "t_resolved": None, "t_turn_end": None, "model_note": None,
        "ready_model": None, "events": 0,
    }

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
            st["events"] += 1
            kind = evt.get("type")
            print(f"[child] {json.dumps(evt)[:400]}", flush=True)

            if kind == "ready":
                st["ready_model"] = evt.get("model")
                st["model_note"] = evt.get("model_note")
                send({"type": "prompt", "id": "p1", "message": PROMPT})

            elif kind == "user_input_requested" and not st["asked"]:
                st["asked"] = True
                st["ask_id"] = evt.get("id")
                st["t_ask"] = time.time()
                # THE WHOLE POINT: abort, never answer.
                st["t_abort"] = time.time()
                send({"type": "abort", "id": "a1"})

            elif kind == "user_input_resolved":
                st["resolved"] = evt
                st["t_resolved"] = time.time()

            elif kind == "response" and evt.get("id") == "a1":
                st["abort_reply"] = evt

            elif kind == "turn_end":
                st["turn_end"] = True
                st["t_turn_end"] = time.time()
                return

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline and not st["turn_end"]:
        time.sleep(0.25)
    _closer(child)
    return st


def arm_idle(model: str | None, timeout: float) -> dict:
    """The control: abort with no turn running must report stopped:false."""
    child = spawn(model)
    st: dict = {"abort_reply": None, "ready": False, "t_abort": None, "t_reply": None}

    def pump() -> None:
        for raw in child.stdout:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = evt.get("type")
            print(f"[child] {json.dumps(evt)[:300]}", flush=True)
            if kind == "ready" and not st["ready"]:
                st["ready"] = True
                st["t_abort"] = time.time()
                print('[-> child] {"type": "abort", "id": "a2"}', flush=True)
                child.stdin.write(json.dumps({"type": "abort", "id": "a2"}) + "\n")
                child.stdin.flush()
            elif kind == "response" and evt.get("id") == "a2":
                st["abort_reply"] = evt
                st["t_reply"] = time.time()
                return

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.time() + timeout
    while time.time() < deadline and st["abort_reply"] is None:
        time.sleep(0.25)
    _closer(child)
    return st


def _ms(a, b) -> str:
    return "n/a" if (a is None or b is None) else f"{(b - a) * 1000:.0f} ms"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=float, default=240.0)
    args = ap.parse_args()

    print("==== ARM 1: parked in an ask ====", flush=True)
    a1 = arm_parked(args.model, args.timeout)
    print("\n==== ARM 2: idle child ====", flush=True)
    a2 = arm_idle(args.model, 90.0)

    print("\n==== RESULT ====", flush=True)
    print(f"ready model               : {a1['ready_model']}", flush=True)
    print(f"ready model_note          : {a1['model_note']}", flush=True)
    print(f"events seen               : {a1['events']}", flush=True)
    print(f"model called the tool     : {a1['asked']}  (ask id {a1['ask_id']})", flush=True)
    print(f"user_input_resolved       : {json.dumps(a1['resolved']) if a1['resolved'] else None}",
          flush=True)
    print(f"  latency abort->resolved : {_ms(a1['t_abort'], a1['t_resolved'])}", flush=True)
    print(f"abort reply               : {json.dumps(a1['abort_reply']) if a1['abort_reply'] else None}",
          flush=True)
    print(f"turn ended                : {a1['turn_end']}", flush=True)
    print(f"  latency abort->turn_end : {_ms(a1['t_abort'], a1['t_turn_end'])}", flush=True)
    print(f"IDLE abort reply          : {json.dumps(a2['abort_reply']) if a2['abort_reply'] else None}",
          flush=True)
    print(f"  latency abort->reply    : {_ms(a2['t_abort'], a2['t_reply'])}", flush=True)

    if a1["events"] == 0:
        print("\nThe child emitted NOTHING — it never started, or its stdout never "
              "reached this script. This says nothing about the model or the wire.",
              flush=True)
        return 3
    if not a1["asked"]:
        print("\nThe model never called ask_user_question. A MODEL outcome, not a wire "
              "failure — re-run or use a model that follows the instruction.", flush=True)
        return 2

    ok = (
        a1["resolved"] is not None
        and a1["resolved"].get("cancelled") is True
        and a1["turn_end"]
        and isinstance(a1["abort_reply"], dict)
        and a1["abort_reply"].get("result", {}).get("stopped") is True
        and isinstance(a2["abort_reply"], dict)
        and a2["abort_reply"].get("result", {}).get("stopped") is False
    )
    print(f"\nBOTH ARMS PASS            : {ok}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
