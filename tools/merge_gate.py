"""Focused local merge check; never merges, installs hooks, or runs the full suite.

The reviewer supplies the affected domain and existing neighboring tests. This
checks that evidence's shape and commit identity, not its authenticity or coverage.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import run_all

# Both were red on main unnoticed until a neighboring check on 2026-10-07.
# Keep this small incident-derived floor, not a guessed dependency map.
REGRESSION_NEIGHBORS = (
    "tests/test_autoscroll.py",
    "tests/test_sidecar_jobs_t1082.py",
)


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ValueError(f"git {' '.join(args)} (exit {result.returncode}): "
                         f"{result.stdout.strip()} {result.stderr.strip()}")
    return result.stdout.strip()


def commit(ref: str) -> str:
    return git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")


def snapshot() -> str:
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("worktree must be clean; commit the preview before checking it")
    return commit("HEAD")


def test_path(value: str) -> Path:
    if not isinstance(value, str):
        raise TypeError("test paths must be strings")
    path = (ROOT / value).resolve()
    if path.parent != ROOT / "tests" or not path.name.startswith("test_") or path.suffix != ".py":
        raise ValueError(f"expected an explicit tests/test_*.py file: {value}")
    if not path.is_file():
        raise ValueError(f"missing test file: {value}")
    return path


def reviewed_neighbors(evidence: Path, base: str, candidate: str) -> tuple[dict, set[Path]]:
    note = json.loads(evidence.read_text(encoding="utf-8"))
    if not isinstance(note, dict):
        raise TypeError("review evidence must be an object")
    for field in ("domain", "review"):
        if not isinstance(note.get(field), str) or not note[field].strip():
            raise ValueError(f"review evidence requires nonempty {field}")
    if note.get("base") != base or note.get("candidate") != candidate:
        raise ValueError("review evidence base/candidate must match the resolved full commit IDs")
    neighbors = note.get("neighbors")
    if not isinstance(neighbors, list) or not neighbors:
        raise ValueError("review evidence requires a nonempty existing-neighbor selection")
    exceptions = note.get("exceptions")
    if not isinstance(exceptions, list) or any(not isinstance(x, str) or not x.strip() for x in exceptions):
        raise ValueError("review evidence requires an explicit exceptions list (empty is allowed)")
    paths = {test_path(value) for value in neighbors}
    for path in paths:
        # A newly added regression alone is not an existing neighboring test.
        relative = path.relative_to(ROOT).as_posix()
        entry = git("ls-tree", base, "--", relative)
        if not entry.startswith(("100644 blob ", "100755 blob ")):
            raise ValueError(f"neighbor must be an existing regular test file at base: {relative}")
    return note, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="target tip before integration")
    parser.add_argument("--candidate", required=True, help="reviewed worker tip")
    parser.add_argument("--review-evidence", required=True, type=Path)
    parser.add_argument("tests", nargs="*", help="additional affected/new test files")
    args = parser.parse_args(argv)
    try:
        head = snapshot()
        base, candidate = commit(args.base), commit(args.candidate)
        for ancestor in (base, candidate):
            git("merge-base", "--is-ancestor", ancestor, head)
        note, neighbors = reviewed_neighbors(args.review_evidence, base, candidate)
        selected = neighbors | {test_path(p) for p in (*REGRESSION_NEIGHBORS, *args.tests)}
        # Reuse the runner's reviewed script inventory; never infer a silent
        # script pass just because a selected file has no pytest functions.
        pytest_files, script_files = run_all.explicit_inventory()
        pyt = sorted(selected.intersection(pytest_files))
        scr = sorted(selected.intersection(script_files))
        if selected != set(pyt + scr):
            raise ValueError("selected files are missing from the runner inventory")
        print(f"Focused merge check: HEAD={head}\nbase={base}\ncandidate={candidate}")
        print(f"Domain: {note['domain']}\nReview: {note['review']}")
        print(f"Explicit exceptions: {json.dumps(note['exceptions'])}")
        for path in sorted(selected):
            print(f"  {path.relative_to(ROOT).as_posix()}")
        print("Only the selected tests run; coverage completeness remains a review responsibility.", flush=True)
        git("diff", "--check", base, head)
        env = dict(os.environ)
        # Extra positional paths in inherited/configured addopts would silently
        # expand this focused command to the full suite.
        env.pop("PYTEST_ADDOPTS", None)
        env.update(LITETUI_NO_HARNESS="1", LITETUI_BACKEND="lmstudio", PYTHONUTF8="1")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "tests"), str(ROOT / "src"), env.get("PYTHONPATH", "")]
        )
        commands = []
        if pyt:
            commands.append([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider",
                             "-p", "readiness_collection_gate", *map(str, pyt)])
        commands.extend([sys.executable, str(path)] for path in scr)
        for command in commands:
            result = subprocess.run(command, cwd=ROOT, env=env, timeout=300, check=False)
            if result.returncode:
                print(f"FAIL: selected test command exited {result.returncode}", flush=True)
                return 1
        if snapshot() != head:
            raise ValueError("HEAD changed during checks; rerun on the final integrated tree")
        print(f"PASS: focused merge check for {head}; full suite NOT RUN; no merge performed.")
        return 0
    except (ValueError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
