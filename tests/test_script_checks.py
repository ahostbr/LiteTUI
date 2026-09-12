"""A disk-derived file list must not be able to kill the run (T699).

`tests/test_footer.py` and `tests/test_settings_live.py` are named `test_*` and
were scripts: one ended in a module-level `sys.exit(...)`, the other in
`asyncio.run(main())` whose `main` did the same. Importing either during
collection raised `SystemExit`, which pytest reports as INTERNALERROR — and
that takes down the WHOLE invocation, not just the offending file. Anyone
assembling a list from disk (`grep -rln <symbol> tests/`) picked them up and
got no results for anything they asked for.

🔴 `collect_ignore` WAS THE OBVIOUS FIX AND IT DOES NOT WORK. Measured
2026-09-12: it is consulted when pytest WALKS A DIRECTORY, and a path given as
a command-line argument takes another route — which is exactly how a
disk-derived list arrives. A `pytest_ignore_collect` hook did not stop it
either. Both left the crash in place for the only invocation the card is about.

⬜ SO THE SIDE EFFECT MOVED, NOT THE COLLECTION RULE. The tally and the exit in
both files sit behind `if __name__ == "__main__":`, their checks are untouched,
and each exposes one arm that reads the result. They still behave identically
when run as scripts — and their 48 assertions now execute under pytest for the
first time since they were written.

⚠️ WHY THIS FILE STILL EXISTS AFTER THAT FIX: the arm below drives the real
invocation, from the outside. Every other arm in the suite is inside the
process whose collection is the thing in question, and a collection failure is
not something a test inside that collection can observe.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONVERTED = ("tests/test_footer.py", "tests/test_settings_live.py")


def test_a_disk_derived_list_that_INCLUDES_them_still_runs() -> None:
    """THE CARD'S SUBJECT, driven as a real subprocess.

    The two former scripts plus a normal module, passed file by file exactly as
    a `grep -rln` list hands them over — the invocation that used to return
    nothing at all.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *CONVERTED, "tests/test_convo_settings.py"],
        capture_output=True, text=True, timeout=600, cwd=str(HERE.parent), check=False,
    )
    out = proc.stdout + proc.stderr
    assert "INTERNALERROR" not in out, f"a disk-derived list still aborts:\n{proc.stdout[-3000:]}"
    assert proc.returncode == 0, f"{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}"
    # ⭐ THE VALIDITY GATE. "No crash" is also what a run that collected NOTHING
    # looks like, and an over-broad ignore would produce exactly that: green,
    # silent, and measuring an empty suite.
    assert " passed" in proc.stdout, proc.stdout[-2000:]


def test_both_files_still_work_as_STANDALONE_SCRIPTS() -> None:
    """The other half of the contract, and the thing the conversion risked.

    Their authors run these by hand — that is what the printed `N/N passed`
    tally is for. A conversion that made them importable while breaking
    `python tests/test_footer.py` would trade one silent loss for another.
    """
    for rel in CONVERTED:
        proc = subprocess.run(
            [sys.executable, rel],
            capture_output=True, text=True, timeout=180, cwd=str(HERE.parent), check=False,
        )
        assert proc.returncode == 0, f"{rel}:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
        tally = [ln for ln in proc.stdout.splitlines() if "passed" in ln and "/" in ln]
        assert tally, f"{rel} printed no N/N tally:\n{proc.stdout[-2000:]}"
        ran, _, _ = tally[-1].strip().partition("/")
        assert int(ran) > 0, f"{rel} reported {tally[-1].strip()}"
