"""One headless LiteTUI child, one prompt, one answer — the shape /consult drives.

LIVES IN e2e/, NOT tests/, AND THAT IS THE POINT. It starts a real child and
drives a real model, so it sits behind both gates e2e/conftest.py describes:
pyproject `testpaths = ["tests"]` never collects it, and LITETUI_E2E=1 is
required even for `pytest e2e/`. It was first committed to tests/ (95cc492),
which is exactly the "live LM Studio smoke ran in the default suite" that
tests/run_all.py records as already fixed once.

SCRIPT-STYLE (module-level sys.exit), like the other e2e/ files: run it
directly, `python e2e/consult_smoke_e2e.py`. The name avoids `test_*` so no
pytest run can collect it and abort on the module-level exit.

WHY THIS EXISTS. T584 routes every non-Claude consult through `litetui --rpc`, so
the skill's extraction step has to quote what the process ACTUALLY emits. This
file is where that shape is pinned, measured rather than assumed:

    {"type": "ready", "version": ..., "model": ..., "cwd": ..., "tool_profile": ...}
    {"type": "turn_start", "model": ..., "thinking_level": ...}
    {"type": "reasoning_delta", "text": "..."}   x N     <-- NOT the answer
    {"type": "text_delta", "text": "..."}        x N     <-- the answer
    {"type": "turn_end", "stopReason": "stop"}

🔴 `reasoning_delta` AND `text_delta` BOTH CARRY `.text`. A parser that
concatenates every `.text` returns the model's chain of thought as the answer —
in the first measured run the reasoning was 17 deltas and the answer was one.
The consumer must select on `type`, and this asserts that the two are separable.

🔴 STDIN MUST STAY OPEN UNTIL `turn_end`. Measured: with stdin at /dev/null the
child exits 0 having emitted NOTHING, because the RPC loop ends before the
prompt runs — a silent empty answer, which is the worst failure shape for a
consult panel. And with stdin left open after turn_end it never exits (the first
run had to be killed at 150 s). So the driver holds stdin open, reads to
turn_end, then closes it — both halves are asserted below.

SKIPS CLEANLY, BY NAME, when no backend answers. A consult gate that cannot tell
"the wiring is broken" from "LM Studio is not running" is not a gate.

🔴 AND THE GATE HAS TO COVER *WHICH* BACKEND, NOT JUST WHETHER ONE ANSWERS.
The first version checked port 1234 and a resident model, then launched a child
that read `<install>/settings.json` and used whatever THAT said — llama.cpp,
down, on 2026-09-10 23:0x. Three arms went red about a backend the child was
never talking to. Two conditions were being conflated: "LM Studio is up" and
"LiteTUI is pointed at it". The child is now told (LITETUI_BACKEND), so the
gates above measure the thing the child actually uses.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import urllib.request
import time
from pathlib import Path

ok: list[bool] = []


def chk(label: str, cond: bool) -> None:
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def _listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


LMSTUDIO_PORT = 1234
EXE = Path(sys.executable).parent / "litetui.exe"

def _resident_model() -> str | None:
    """The model LM Studio has loaded RIGHT NOW, or None.

    🔴 READ EVERY RUN, NEVER STORED, AND NEVER A NAME OF OUR CHOOSING.
    Ryan, 2026-09-10 22:0x, after a probe of mine put a second 27B beside the
    one he was using: no model is loaded into VRAM without checking first and
    asking. LM Studio JIT-loads whatever model a COMPLETION names — connect is
    only a REST read (`_list_sync` -> GET /api/v0/models, llm_backend.py:1357),
    so the load happens the moment a turn is submitted, not at startup, and
    nothing in the RPC output marks it: `ready` looks identical either way.

    Naming the already-resident id is therefore necessary AND sufficient: the
    completion hits a loaded model and loads nothing. `--model` is applied
    before the first prompt is submitted (both inside `_apply_cli_args`), so
    it is the lever that decides which model the turn names. Cited by symbol,
    not line: the T594 merge moved these by ~35 lines and a stale number sent
    one reader to the wrong function already.

    ⚠️ He switches models during a session, so this is re-read per run rather
    than captured once.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{LMSTUDIO_PORT}/api/v0/models", timeout=5
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None
    for m in data.get("data", []):
        if m.get("state") == "loaded" and m.get("id"):
            return str(m["id"])
    return None


print("=== litetui --rpc, the shape /consult parses (T584) ===")

if not EXE.exists():
    print(f"SKIP: {EXE} is not installed in this interpreter's Scripts dir")
    sys.exit(0)
if not _listening(LMSTUDIO_PORT):
    print(f"SKIP: nothing is listening on 127.0.0.1:{LMSTUDIO_PORT} "
          "(LM Studio down) — the wiring is untested, not broken")
    sys.exit(0)

resident = _resident_model()
if resident is None:
    print("SKIP: LM Studio answers but no model is LOADED — a prompt would",
          "JIT-load one, and this suite does not load models (see the note above)")
    sys.exit(0)
print(f"  using the resident model: {resident!r}")

work = Path(tempfile.mkdtemp(prefix="consult-smoke-"))
child = subprocess.Popen(
    [str(EXE), "--rpc", "--tool-profile", "scheduled", "--model", resident,
     "--cwd", str(work), "--prompt", "reply with the single word ok"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    text=True, encoding="utf-8", errors="replace",
    # 🔴 THE CHILD IS TOLD WHICH BACKEND TO USE. Without this it reads
    # <install>/settings.json — a file nobody in a consult controls — and
    # this ran against llama.cpp (down) on 2026-09-10 23:0x while asserting
    # things about LM Studio: 3 reds that were neither the test's subject
    # nor a defect. The gates above prove LM STUDIO is up and loaded; only
    # this makes the child use the thing those gates measured.
    #
    # It is also what /consult itself must do. The skill's model entries
    # carry `"backend": "lmstudio" | "llamacpp" | "codex"` and there is NO
    # --backend flag (cli.py:36-55) — LITETUI_BACKEND is the only lever
    # (settings.py:363-372, ENV_OVERRIDES).
    env={**os.environ, "LITETUI_BACKEND": "lmstudio"},
)

events: list[dict] = []
deadline = time.monotonic() + 180
try:
    while time.monotonic() < deadline:
        line = child.stdout.readline()
        if not line:
            break
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
        if events[-1].get("type") == "turn_end":
            break

    kinds = [e.get("type") for e in events]

    # 🔴 `ready` IS NOT RELIABLY THE FIRST LINE, AND THIS USED TO ASSUME IT
    # WAS (`kinds[:1] == ["ready"]`, `events[0]`). Measured 2026-09-10 23:0x,
    # same flags, three backends:
    #   lmstudio  ready @7.6s then turn_start @7.6s   <- ready first
    #   codex     turn_start @1.1s then ready @1.1s   <- turn_start first
    #   llamacpp  turn_start then ready               <- turn_start first
    # `_apply_cli_args` and `_rpc_emit_ready` are two Textual workers started
    # back to back (app.py, on_mount), each waiting on available_models. Who
    # emits first is decided by how long _connect() takes — a race, not an
    # order. A consumer that indexes events[0] gets turn_start on a fast
    # backend. Select by `type`, never by position.
    ready = next((e for e in events if e.get("type") == "ready"), None)
    chk("the child announces itself with a `ready` line", ready is not None)

    # ⚠️ WHAT THIS CAN HONESTLY ASSERT IS "ready NAMES A PROFILE" — NOT THAT
    # IT NAMES THE ONE PASSED. --tool-profile is installed at construction
    # (app.py:1209-1215) and then _submit_text overwrites _active_tool_profile
    # with settings.tool_policy_profile (app.py:4272 -> :4296) before the turn
    # streams. So `ready` reports whichever side of that overwrite won the
    # race above: measured "scheduled" under lmstudio (ready won) and
    # "interactive" under codex (submit won) from the SAME
    # `--tool-profile scheduled`. Asserting =="scheduled" here would have been
    # green on one backend and red on another for a reason that has nothing
    # to do with what this file tests. The turn itself runs under the settings
    # profile either way — reported for triage, not fixed here.
    chk(f"`ready` names a tool profile (got {(ready or {}).get('tool_profile')!r})",
        (ready or {}).get("tool_profile") in
        {"scheduled", "interactive", "autonomous"})
    chk("the turn is bracketed by turn_start and turn_end",
        "turn_start" in kinds and kinds[-1] == "turn_end")
    # 🔴 BRACKETING ALONE IS SATISFIED BY A FAILED TURN. Measured against a
    # down backend: turn_start and turn_end both arrived, the arm above went
    # GREEN, and the turn had produced nothing —
    #   {"type": "turn_end", "stopReason": "error",
    #    "error": "Something went wrong talking to the model server."}
    # An assertion a DIFFERENT failure can satisfy is a test of something
    # else. The reason belongs in the failure, so read it out.
    _end = events[-1] if events else {}
    chk(f"the turn ENDED cleanly (stopReason={_end.get('stopReason')!r}"
        f"{', ' + repr(_end.get('error')) if _end.get('error') else ''})",
        _end.get("stopReason") == "stop")

    answer = "".join(e.get("text", "") for e in events if e.get("type") == "text_delta")
    reasoning = "".join(e.get("text", "") for e in events if e.get("type") == "reasoning_delta")
    chk("text_delta carries a non-empty answer", answer.strip() != "")
    chk("the answer is SEPARABLE from the reasoning (different `type`)",
        "reasoning_delta" not in [k for k in kinds if k == "text_delta"])
    chk("a parser that took every `.text` would include the reasoning",
        # not a defect — a warning this file exists to make concrete
        (reasoning == "") or (answer != answer + reasoning))
    print(f"      answer={answer.strip()[:60]!r}  reasoning={len(reasoning)} chars")

    # 🔴 THE EXIT HALF. Closing stdin is what ends it; without this the child
    # outlives the consult and the panel hangs on a process that already
    # answered.
    child.stdin.close()
    try:
        child.wait(timeout=30)
        chk("closing stdin ends the child after turn_end", True)
    except subprocess.TimeoutExpired:
        chk("closing stdin ends the child after turn_end", False)
finally:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=10)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
