"""The envelope's proof. Two parts.

1. SOURCE SCAN — the structural assertion (Sentinel's requirement, triage
   2026-08-19): an envelope only makes the bug class impossible if NOTHING
   CAN BYPASS IT. Assert that no subprocess.run/Popen/call/check_output
   exists in any runtime module outside ttyguard.py itself. Without this
   assertion, ttyguard is a helper everyone is supposed to remember — the
   same category as an idempotency mechanism that was never checked and
   silently stopped working.

2. BEHAVIOUR — the envelope's guarantees, each against a REAL child
   process (not a mock): decode survival under an undecodable byte,
   DEVNULL stdin (immediate EOF, not a stolen terminal), the terminal
   repair running on BOTH the success path and the timeout-killed path.

Scope: top-level modules of this repo (the TUI runtime). chrome-bridge/ and
pccontrol/ are standalone tools with their own terminal context; they are
excluded from the scan ON PURPOSE, and the exclusion is written here rather
than left to drift.
"""

import re
import subprocess
import sys
from pathlib import Path

import sanitize
import ttyguard

REPO = Path(__file__).resolve().parent

# ── 1. source scan ──────────────────────────────────────────────────────────

_SPAWN = re.compile(r"subprocess\.(run|Popen|call|check_output|check_call)\(")
# The envelope itself, and this file (which must name the pattern to scan for
# it — the regex text does not self-match, but the whitelist is defence in
# depth, not faith).
_WHITELIST = {"ttyguard.py", "test_ttyguard.py"}


def test_no_child_process_escapes_the_envelope():
    violations = []
    for f in sorted(REPO.glob("*.py")):
        if f.name in _WHITELIST:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if _SPAWN.search(line):
                violations.append(f"{f.name}:{i}: {line.strip()[:90]}")
    assert not violations, (
        "raw subprocess call outside the ttyguard envelope:\n"
        + "\n".join(violations)
        + "\nRoute it through ttyguard.run/popen — that is the whole point."
    )


# ── 2. behaviour, against real children ─────────────────────────────────────

def _child(bytes_seq: str) -> list:
    """A child that writes raw bytes to stdout — the incident shape."""
    code = (
        "import sys\n"
        f"sys.stdout.buffer.write({bytes_seq})\n"
        "sys.stdout.buffer.flush()\n"
    )
    return [sys.executable, "-c", code]


def test_undecodable_byte_does_not_lose_the_output():
    # 0x9b is not a valid UTF-8 start byte. Under strict decoding (the old
    # default) in a UTF-8 locale, the reader thread dies and the output is
    # lost — "(no output)" instead of the child's real words. Under the
    # envelope it becomes U+FFFD and BOTH neighbours survive.
    r = ttyguard.run(_child("b'BEFORE' + bytes([0x9b]) + b'AFTER'"))
    assert r.returncode == 0, f"child died: rc={r.returncode} err={r.stderr!r}"
    assert "BEFORE" in (r.stdout or ""), "output before the bad byte was lost"
    assert "AFTER" in (r.stdout or ""), "output after the bad byte was lost"


def test_stdin_is_devnull_by_default():
    # A child that reads stdin with an INHERITED TUI stdin would block on
    # (or steal) the user's terminal. DEVNULL gives it an immediate EOF.
    r = ttyguard.run([sys.executable, "-c",
                      "import sys; print(len(sys.stdin.read()))"])
    assert r.returncode == 0
    assert r.stdout.strip() == "0", (
        f"child read {r.stdout.strip()!r} from stdin — expected immediate EOF "
        "(DEVNULL), not a live terminal"
    )


def test_timeout_kills_the_child_but_still_repairs_the_terminal():
    calls = []
    orig = sanitize.reset_terminal_modes
    sanitize.reset_terminal_modes = lambda: calls.append(1)
    try:
        try:
            ttyguard.run([sys.executable, "-c", "import time; time.sleep(30)"],
                         timeout=1)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("expected TimeoutExpired")
    finally:
        sanitize.reset_terminal_modes = orig
    assert calls, (
        "terminal repair did not run after a timeout-killed child — a child "
        "that flipped modes and died by timeout is exactly the case that "
        "needs it"
    )


def test_success_path_repairs_the_terminal_exactly_once():
    calls = []
    orig = sanitize.reset_terminal_modes
    sanitize.reset_terminal_modes = lambda: calls.append(1)
    try:
        r = ttyguard.run([sys.executable, "-c", "print('ok')"])
    finally:
        sanitize.reset_terminal_modes = orig
    assert r.returncode == 0
    assert len(calls) == 1, f"expected exactly one repair, got {len(calls)}"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"=== {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
