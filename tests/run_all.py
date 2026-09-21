"""Run every test with the runner it actually needs.

tests/ holds TWO styles and they are mutually hostile:

  * pytest-style — `def test_*` collected by pytest.
  * script-style — top-level code ending in `sys.exit(0 if all(ok) else 1)`.

A module-level `sys.exit()` raises SystemExit during pytest COLLECTION, which
aborts the whole run with INTERNALERROR and reports "no tests ran" — not a
failure of that one file, a failure of everything. And a pytest-style file run
as `python tests/x.py` imports fine and asserts nothing, exiting 0: a silent
pass that covers nothing.

So the split is not cosmetic. Either runner applied to the wrong half produces a
confident, wrong answer.

DISCRIMINATOR: a module-level `sys.exit(` means script-style. Three earlier
guesses were wrong — "imports pytest" missed files using bare asserts, and
"asyncio.run(main())" missed the ones that just fall off the end into sys.exit.

Usage:  python tests/run_all.py            (from anywhere)
        python tests/run_all.py -v         (show each file)
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

# There is deliberately no SLOW set here any more.
#
# This file used to declare `SLOW: set[str] = set()` above a comment promising
# "excluded by default; pass --all to include them". The set was never read and
# `--all` was never parsed — the only flag this runner has ever handled is -v.
# So the quarantine existed on paper, a live LM Studio smoke test ran in the
# default suite regardless, and anyone auditing the runner saw a facility and
# stopped looking. A declared-but-inert mechanism is worse than an absent one
# for exactly that reason: absence looks unfinished, decoration looks done.
#
# Live-service tests now live under e2e/, gated twice and for real: they are
# outside pytest's `testpaths`, and e2e/conftest.py skips them unless
# LITETUI_E2E=1. Both gates were verified with a control proving they can open.


#: Definitions whose bodies do NOT run at import.
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

#: Calls that end or hijack the interpreter when they run at import.
_EXITERS = {
    ("sys", "exit"), ("os", "exit"), ("os", "_exit"),
    ("", "exit"), ("", "quit"), ("asyncio", "run"),
}


def _is_main_guard(node: ast.AST) -> bool:
    """`if __name__ == "__main__":` — the one place an exit belongs."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _flat(node: ast.AST):
    """Every node under `node` EXCEPT the bodies of nested defs.

    ⚠️ THIS IS THE SECOND HALF OF THE PRUNE, NOT THE WHOLE OF IT, AND MY OWN
    COMMENT SAID OTHERWISE UNTIL A MUTATION PROVED IT WRONG. The comment here
    read "THE PRUNE IS THE WHOLE RULE"; deleting this `continue` and running
    the suite changed nothing, because `module_level_hazards` already skips a
    top-level `def` before it ever calls this. What the earlier scan got wrong
    (28 files flagged, 10 real — T700) was the OUTER filter, not this one.

    What this covers is the narrower case the outer filter cannot see: a `def`
    nested inside a module-level `try`, `if` or `with`, whose body also does
    not run at import. `test_the_prune_covers_a_def_nested_in_a_module_level_
    block` in `test_script_checks.py` is that case, added because a line no
    test can kill is a line nobody knows is load-bearing.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFS):
            continue
        yield from _flat(child)


def module_level_hazards(tree: ast.AST) -> list[str]:
    """What this module does AT IMPORT that breaks pytest collection.

    🔴 ONE RULE, ONE HOME (T702). `tests/test_script_checks.py` imports this
    rather than carrying its own copy: two detectors answering the same
    question are two answers that can disagree, and the day they do, the one
    nobody is looking at is the one that is right. An arm in that file asserts
    they are literally the same function.

    ⬜ `if __name__ == "__main__":` IS EXEMPT, AND THAT IS THE T702 FIX. This
    used to walk the whole tree, so a guarded exit still counted — which left
    thirteen files filed as script-style that pytest can collect perfectly
    well, `tests/test_ttyguard.py` among them, guarded since before any of
    this. The comment that said it "must run as a script" was a description of
    this blind spot, not a requirement.

    ⚠️ IT COVERS `raise SystemExit(...)` TOO. `test_chrome_tool.py` carries
    that spelling inside a fake TRACEBACK — a string literal — which is why
    this is an AST rule and was never a substring one: the text check filed
    that file as script-style and silently stopped running its nine
    assertions.
    """
    hazards = []
    for stmt in getattr(tree, "body", []):
        if isinstance(stmt, _DEFS) or _is_main_guard(stmt):
            continue
        for node in _flat(stmt):
            if isinstance(node, ast.Raise):
                exc = node.exc
                name = getattr(exc, "func", exc)
                if isinstance(name, ast.Name) and name.id == "SystemExit":
                    hazards.append(f"{node.lineno} raise SystemExit")
                continue
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = getattr(getattr(func, "value", None), "id", "")
            if (owner, attr) in _EXITERS:
                hazards.append(f"{node.lineno} {owner + '.' if owner else ''}{attr}")
    return hazards


def _exits(tree: ast.AST) -> bool:
    """Does importing this module end the interpreter or spin a loop?"""
    return bool(module_level_hazards(tree))


def _has_tests(tree: ast.AST) -> bool:
    """Would pytest find anything to CALL in this module?

    🔴 CLASSES COUNT, AND LEAVING THEM OUT SILENTLY UNMEASURED 83 TESTS.
    This asked only for a module-level `def test_*` (`tree.body`), so six files
    whose tests live in `class Test*` — test_input_history, test_rpc,
    test_send_resolve, test_subagent, test_thinking_collapse,
    test_thinking_probe — were filed script-style. Run as
    `python tests/x.py` each one imports, defines its classes, calls nothing,
    prints nothing and exits 0. The runner then reports `ok`.

    Measured 2026-09-10 at origin/main ccf0229:
      pytest --collect-only on those six  -> 83 tests collected
      python tests/test_thinking_probe.py -> no output, exit 0

    ⚠️ AND THE ONE RUNNER MEASURED LESS THAN THE WRONG ONE. `conftest.py`'s
    `collect_ignore` uses a SUBSTRING check (`"def test_" not in src`) which
    matches an indented method, so a plain `pytest tests/` collects all six and
    run_all did not. This file's own docblock claimed "Two rules, no gap"; the
    gap was exactly here, and the shape of it is the hazard the docblock warns
    about one paragraph earlier — a pytest-style file run as a script "imports
    fine and asserts nothing, exiting 0: a silent pass that covers nothing."

    `class Test*` is pytest's own default (`python_classes = Test*`), so this
    matches what the collector actually does rather than a guess about it.
    """
    for node in tree.body:
        # ⚠️ THE TWO BRANCHES BELOW LOOK SYMMETRIC AND ARE NOT. This one keeps
        # its nested `if` on purpose: it is followed by an `elif`, so collapsing
        # it into `if isinstance(...) and node.name.startswith("test_")` would
        # send a module-level function NOT named test_* on to the ClassDef
        # branch — a behaviour change in the classifier, not a tidy-up. ruff
        # flags only the other one, correctly; do not "fix" this to match.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                return True
        elif (
            isinstance(node, ast.ClassDef)
            and node.name.startswith("Test")
            and any(
                isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                and m.name.startswith("test_")
                for m in node.body
            )
        ):
            return True
    return False


def classify() -> tuple[list[Path], list[Path]]:
    pytest_style, script_style = [], []
    for f in sorted(TESTS.glob("test_*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        # TWO conditions, because either alone misclassifies a real file:
        #   * a module-level exit (either spelling) kills pytest COLLECTION and
        #     aborts the entire run with INTERNALERROR — one bad file reports
        #     "no tests ran" for everything.
        #     ⬜ "MODULE-LEVEL" NOW MEANS IT (T702). This used to count an exit
        #     anywhere in the file, including under `if __name__ ==
        #     "__main__":`, so thirteen files pytest can collect perfectly well
        #     were filed as scripts — `test_ttyguard.py` among them, guarded
        #     since long before this. The note that used to sit here said it
        #     "must run as a script", which described the blind spot rather
        #     than a requirement.
        #   * no module-level `def test_*` means pytest would collect nothing
        #     and report a silent pass covering zero assertions.
        # 🔴 THE STATEMENT, NEVER THE STRING. This was a substring check, and
        # test_chrome_tool.py carries `raise SystemExit(...)` inside a fake
        # TRACEBACK FIXTURE — a string literal in a test. The check matched
        # those characters, filed the file as script-style, and its nine
        # assertions silently stopped running under pytest.
        #
        # The parse below was already happening for the other half of this
        # decision, so the AST cost nothing and was simply not used here.
        try:
            tree = ast.parse(src)
            has_tests = _has_tests(tree)
            exits = _exits(tree)
        except SyntaxError:
            has_tests = False
            # Unparseable: fall back to the old blunt check rather than
            # claiming it is safe to collect.
            exits = "sys.exit(" in src or "raise SystemExit(" in src
        (script_style if (exits or not has_tests) else pytest_style).append(f)
    return pytest_style, script_style


# Reviewed legacy-only exceptions, never inferred from absent test definitions.
# Read-only scan at 4eccbbb found zero script-classified files. New exceptions
# require explicit review; all other files go through pytest item accounting.
LEGACY_SCRIPT_TESTS: frozenset[str] = frozenset()


def explicit_inventory() -> tuple[list[Path], list[Path]]:
    files = {path.name: path for path in TESTS.glob("test_*.py")}
    missing = LEGACY_SCRIPT_TESTS - files.keys()
    if missing:
        raise ValueError(f"Declared legacy scripts missing: {sorted(missing)}")
    pyt, scr = [], []
    for name, path in sorted(files.items()):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError, OSError) as exc:
            raise ValueError(f"Unreadable/syntax-invalid test {name}: {exc}") from exc
        if name in LEGACY_SCRIPT_TESTS:
            scr.append(path)
        elif module_level_hazards(tree):
            raise ValueError(f"Import hazard requires explicit inventory review: {name}")
        else:
            pyt.append(path)
    return pyt, scr


def _run_bounded(command, *, timeout, **kwargs):
    """Report child timeout as failure without abandoning remaining files.

    subprocess.run kills/waits for the direct child on timeout. This is not
    a process-tree cleanup guarantee; tests must still own their descendants.
    """
    try:
        return subprocess.run(command, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        message = f"TIMEOUT after {timeout}s: {command[-1]}"
        print(message)
        return subprocess.CompletedProcess(command, 124, "", message)


def main() -> int:
    verbose = "-v" in sys.argv

    # Children inherit this. See tests/conftest.py for why: a test run used to
    # evict the RUNNING app from the harness registry. conftest covers pytest;
    # this covers the script-style files, which never import it.
    os.environ.setdefault("LITETUI_NO_HARNESS", "1")

    # Same shape, newer hazard: this box has BOTH engines installed, so a
    # fresh Settings() would push the first-boot ENGINE PICKER modal over
    # every script-style test's input. An env-pinned backend is the product's
    # own "nothing to ask" path (conftest covers the pytest half).
    os.environ.setdefault("LITETUI_BACKEND", "lmstudio")

    # 🔴 A child's stdout is a PIPE, so on Windows it encodes as cp1252 — and
    # every script-style test that prints the red-circle emoji in a label
    # crashed with UnicodeEncodeError AFTER its checks had all passed, so the
    # suite reported eight false failures. UTF-8 mode makes the child's pipes
    # use UTF-8 regardless of the console code page. (The parent side decodes
    # the same way — see the encoding= below.)
    os.environ.setdefault("PYTHONUTF8", "1")

    # 🔴 AND THE ONE conftest DOES THAT THIS DID NOT: src/ ON THE PATH.
    #
    # conftest.py inserts `<repo>/src` into sys.path so `import litetui` resolves
    # for the pytest half. The script-style files never import conftest, and a
    # bare `python tests/x.py` has only the repo root on the path — so on any
    # checkout where the package is not separately installed, EIGHT of them died
    # with `ModuleNotFoundError: No module named 'litetui'` before running a
    # single check.
    #
    # It looked like a repo failure and was an ENVIRONMENT one: the same files
    # pass under `uv run` in the primary clone, where the project IS installed,
    # which is why this survived — the runner was only ever used where it
    # happened to work. Set here rather than in each script: this function
    # already owns "what the script half needs that conftest gives the other".
    _src = str(ROOT / "src")
    _existing = os.environ.get("PYTHONPATH", "")
    if _src not in _existing.split(os.pathsep):
        os.environ["PYTHONPATH"] = _src + (os.pathsep + _existing if _existing else "")

    try:
        pyt, scr = explicit_inventory()
    except ValueError as exc:
        print(f"FAILED: {exc}")
        return 1
    if "--scripts-only" in sys.argv:
        pyt = []

    print(f"pytest-style: {len(pyt)}   script-style: {len(scr)}\n")

    if not pyt and not scr:
        print("FAILED: empty selected test inventory; nothing was verified")
        return 1

    failures: list[str] = []

    if pyt:
        # Files are named EXPLICITLY on the command line, which bypasses
        # conftest's collect_ignore — that filter only applies to collection by
        # directory. So the AST partition above is not a tidiness measure, it is
        # the only thing standing between this run and a global zero: one
        # script-style file in `pyt` runs its module body during collection, its
        # sys.exit() fires inside the collector, and pytest reports INTERNALERROR
        # and "no tests ran" for ALL 82 files. Reproduced here, with a control:
        #   pytest tests/test_version.py       -> 9 passed,  exit 0
        #   pytest tests/test_harness_tool.py  -> INTERNALERROR> SystemExit: 0
        #                                         "no tests ran", exit 3
        # Note what that traceback reads like: SystemExit: **0**, and a summary
        # line saying no tests ran. Both look benign. The exit code is the only
        # honest signal, which is why it is interpreted by name below rather
        # than compared to zero in passing.
        # `--durations=25` costs nothing and is the only way anyone finds out
        # WHERE the half hour goes. Without it every run reports one number and
        # a fix has to be aimed by guess; with it the tail is on screen every
        # time. Measured 2026-09-03: the top ten were all ~9s and nearly
        # identical to each other, which is the signature of a fixed per-test
        # cost (app boot / teardown), not of ten slow tests.
        # ⬜ NO `check=True`, DELIBERATELY (ruff PLW1510 points here). `check`
        # raises on a non-zero exit, which would abandon the run at the first
        # failing file and never reach the script half below — the opposite of
        # what a runner whose job is to report EVERY file wants. The return code
        # is read by name immediately after, which is the whole point.
        proc = _run_bounded(
            [sys.executable, "-m", "pytest", "-q", "--durations=25",
             "-p", "readiness_collection_gate", *[str(p) for p in pyt]],
            timeout=3600,
            env={**os.environ, "PYTHONPATH": str(TESTS) + os.pathsep + os.environ["PYTHONPATH"]},
            cwd=str(ROOT),
        )
        # pytest's documented exit codes. NO_TESTS (5) and INTERNAL (3) are
        # spelled out because both are ways of measuring NOTHING while looking
        # unremarkable, and a future edit that treats "nothing collected" as
        # nothing-to-do would turn this whole class of failure back into a pass.
        reason = {
            1: "tests failed",
            2: "run was interrupted",
            3: "INTERNAL ERROR — a script-style file was collected by pytest; "
               "the AST partition in classify() is what prevents this",
            4: "pytest usage error",
            5: "NO TESTS COLLECTED — treated as a failure, never as nothing-to-do",
        }.get(proc.returncode)
        if proc.returncode != 0:
            failures.append(f"pytest suite (exit {proc.returncode}: {reason or 'unknown'})")

    print()
    for f in scr:
        # Same reason as the pytest child above: `check=True` would raise on
        # the first failing script and the remaining files would never run, so
        # the report would name one failure and hide the rest.
        proc = _run_bounded(
            [sys.executable, str(f)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",  # matches the child's PYTHONUTF8 above
            timeout=300,
        )
        ok = proc.returncode == 0
        if not ok:
            failures.append(f.name)
        if verbose or not ok:
            print(f"  {'ok  ' if ok else 'FAIL'}  {f.name}")
            if not ok:
                tail = (proc.stdout + proc.stderr).strip().splitlines()[-6:]
                for line in tail:
                    print(f"        {line}")

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print(f"all green — {len(pyt)} pytest file(s) + {len(scr)} script(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
