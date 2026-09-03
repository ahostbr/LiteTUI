"""Run the tests a change actually touches — the INNER loop, not the gate.

Ryan, 2026-09-03: "theres gotta be a better way than running 45minutes of tests
for every change ... thats unexceptable IMO".

WHAT THIS IS AND IS NOT. This is a fast, DELIBERATELY INCOMPLETE selection for
the edit-run-edit loop. `python tests/run_all.py` remains the pre-push gate and
this does not replace it — it is a heuristic over module NAMES, and a heuristic
cannot know that editing A broke a test that never mentions A. It prints what it
skipped, every time, so nobody can mistake its green for the suite's.

🔴 IT CLASSIFIES WITH `run_all.classify()`, BY IMPORT, NOT WITH A COPY.
tests/ holds two mutually hostile styles, and there are already TWO rules on this
box that partition them differently:

    run_all.classify()          13 script-style — AST: a module-level exit
                                OR no module-level `def test_*`
    conftest.collect_ignore     12 ignored     — text: no `def test_` anywhere

They disagree on test_ttyguard.py, which has both test functions and an exit (its
exit is under `if __name__ == "__main__"`, so pytest importing it is harmless and
that file ends up covered by whichever entry point you use). A THIRD rule here
would be a third answer to drift against, so this imports the first one. If
run_all's discriminator changes, this follows it for free.

⚠️ AND `python -m pytest` IS NOT THE SUITE. conftest's collect_ignore means a
bare pytest run never collects 12 files — including test_ask_user_question.py,
which is the only cover for src/litetui/ask_user_question.py. A selection that
only ever ran pytest would silently skip the one test that matters for a change
to that module. That is why the script half is run here too.

⚠️ THE SELECTION IS ONLY SMALL WHEN THE CHANGE IS. A module everything imports
(side_panel, app) is named by most of tests/, so its "change-scoped" set is most
of the suite and this saves nothing. That is the honest behaviour, not a defect —
use `--list` first and decide, rather than discovering it 20 minutes in.

Usage:
    python tools/changed_tests.py --list          # print the selection, run nothing
    python tools/changed_tests.py                 # working tree vs HEAD
    python tools/changed_tests.py HEAD~1          # since a commit
    python tools/changed_tests.py a2de5eb^..a2de5eb
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
PKG = ROOT / "src" / "litetui"

# The child environment run_all.main() uses, set before anything imports app or
# harness. Same three values, same setdefault, for the same reasons documented
# in tests/conftest.py: no fleet registration, no first-boot engine picker, utf-8.
os.environ.setdefault("LITETUI_NO_HARNESS", "1")
os.environ.setdefault("LITETUI_BACKEND", "lmstudio")
os.environ.setdefault("PYTHONUTF8", "1")

sys.path.insert(0, str(TESTS))
import run_all  # noqa: E402  — the partition, by import, never by copy

#: Tests that walk the PACKAGE rather than one module: a census, an AST scan, an
#: rglob. Any source change can break one of these without naming it, so they are
#: always selected. Derived, not listed — a new census test joins by existing.
CENSUS_PATTERN = re.compile(r"rglob\(|walk_packages|ast\.parse\(")


def census_files() -> set[Path]:
    return {
        f for f in TESTS.glob("test_*.py")
        if CENSUS_PATTERN.search(f.read_text(encoding="utf-8", errors="replace"))
    }


def changed_paths(rev: str | None) -> list[Path]:
    """Changed files. Working tree by default, INCLUDING untracked tests.

    An untracked test is the normal state of a test you just wrote, and leaving
    it out would make this tool blind to the file most likely to be red.
    """
    out: set[Path] = set()
    if rev:
        args = ["git", "diff", "--name-only", rev]
    else:
        args = ["git", "diff", "--name-only", "HEAD"]
    for line in _git(args):
        out.add(ROOT / line)
    if not rev:
        for line in _git(["git", "ls-files", "--others", "--exclude-standard", "tests"]):
            out.add(ROOT / line)
    return sorted(out)


def _git(args: list[str]) -> list[str]:
    proc = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(f"git failed: {' '.join(args)}\n{proc.stderr.strip()}")
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def select(changed: list[Path]) -> tuple[set[Path], set[str]]:
    """(test files to run, module stems that matched nothing).

    The second half is returned rather than dropped: a changed module with NO
    test naming it is a real signal — either the module is uncovered, or its
    cover is one of the census files. Silence there would read as coverage.
    """
    selected: set[Path] = set(census_files())
    modules: list[str] = []
    for p in changed:
        try:
            rel = p.relative_to(ROOT)
        except ValueError:
            continue
        if rel.parts[:1] == ("tests",) and p.name.startswith("test_") and p.exists():
            selected.add(p)
        elif p.suffix == ".py" and PKG in p.parents:
            modules.append(p.stem)

    unmatched: set[str] = set()
    for mod in modules:
        # Word-boundary on the module STEM: `import x`, `from litetui.x`,
        # `litetui.x.y`, `patch("litetui.x...")` all match; `xyz` does not.
        pat = re.compile(rf"\b{re.escape(mod)}\b")
        hits = {
            f for f in TESTS.glob("test_*.py")
            if pat.search(f.read_text(encoding="utf-8", errors="replace"))
        }
        if hits:
            selected |= hits
        else:
            unmatched.add(mod)
    return selected, unmatched


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--list"]
    list_only = "--list" in sys.argv
    rev = args[0] if args else None
    changed = changed_paths(rev)
    if not changed:
        print("no changes — nothing to run")
        return 0

    selected, unmatched = select(changed)
    pyt_all, scr_all = run_all.classify()
    pyt = sorted(selected & set(pyt_all))
    scr = sorted(selected & set(scr_all))
    total = len(pyt_all) + len(scr_all)

    print(f"changed: {len(changed)} file(s)")
    for p in changed:
        print(f"    {p.relative_to(ROOT).as_posix()}")
    print(f"\nselected {len(pyt) + len(scr)} of {total} test files "
          f"({len(pyt)} pytest, {len(scr)} script)")
    if unmatched:
        print("\n⚠️  changed modules NO test file names — uncovered, or covered "
              "only by a census test:")
        for m in sorted(unmatched):
            print(f"    {m}")

    if list_only:
        for f in pyt:
            print(f"    pytest  {f.name}")
        for f in scr:
            print(f"    script  {f.name}")
        print(f"\n{total - len(pyt) - len(scr)} file(s) would NOT run.")
        return 0

    sys.stdout.flush()   # the selection must be visible before a long run
    t0 = time.monotonic()
    failures: list[str] = []

    if pyt:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--durations=10",
             *[str(p) for p in pyt]],
            cwd=str(ROOT),
        )
        # Same exit-code table as run_all: 5 (nothing collected) and 3
        # (INTERNALERROR) are ways of measuring NOTHING while looking calm.
        if proc.returncode != 0:
            reason = {1: "tests failed", 2: "interrupted",
                      3: "INTERNAL ERROR — a script-style file was collected",
                      4: "usage error",
                      5: "NO TESTS COLLECTED — a failure, never nothing-to-do",
                      }.get(proc.returncode, "unknown")
            failures.append(f"pytest (exit {proc.returncode}: {reason})")

    for f in scr:
        print(f"\n[script] {f.name}")
        proc = subprocess.run(
            [sys.executable, str(f)], cwd=str(ROOT),
            capture_output=True, text=True, encoding="utf-8", timeout=300,
        )
        ok = proc.returncode == 0
        print(f"  {'ok  ' if ok else 'FAIL'} (exit {proc.returncode})")
        if not ok:
            failures.append(f.name)
            for line in (proc.stdout + proc.stderr).strip().splitlines()[-8:]:
                print(f"      {line}")

    elapsed = time.monotonic() - t0
    skipped = total - len(pyt) - len(scr)
    print(f"\n{elapsed:.1f}s for {len(pyt) + len(scr)} file(s). "
          f"{skipped} file(s) NOT RUN — this is the inner loop, not the gate.")
    print("Pre-push gate is still:  python tests/run_all.py")

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
