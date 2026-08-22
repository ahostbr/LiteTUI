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

#: Runs against a live model / long-running services. Excluded by default so a
#: red here is never mistaken for a code failure; pass --all to include them.
SLOW: set[str] = set()


def _exits(tree: ast.AST) -> bool:
    """Does this module actually EXIT when imported?

    `raise SystemExit(...)` or a call to `sys.exit(...)`/`exit(...)` as real
    code, anywhere in the file. Text that merely LOOKS like one — a traceback
    quoted in a fixture, a docstring describing the hazard — is not a hazard.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise):
            exc = node.exc
            name = getattr(exc, "func", exc)
            if isinstance(name, ast.Name) and name.id == "SystemExit":
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "exit":
                if isinstance(f.value, ast.Name) and f.value.id == "sys":
                    return True
            elif isinstance(f, ast.Name) and f.id == "exit":
                return True
    return False


def classify() -> tuple[list[Path], list[Path]]:
    pytest_style, script_style = [], []
    for f in sorted(TESTS.glob("test_*.py")):
        src = f.read_text(encoding="utf-8", errors="replace")
        # TWO conditions, because either alone misclassifies a real file:
        #   * a module-level exit (either spelling) kills pytest COLLECTION and
        #     aborts the entire run with INTERNALERROR — one bad file reports
        #     "no tests ran" for everything. test_ttyguard has BOTH an exit and
        #     test functions, and must run as a script.
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
            has_tests = any(
                isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name.startswith("test_")
                for n in tree.body
            )
            exits = _exits(tree)
        except SyntaxError:
            has_tests = False
            # Unparseable: fall back to the old blunt check rather than
            # claiming it is safe to collect.
            exits = "sys.exit(" in src or "raise SystemExit(" in src
        (script_style if (exits or not has_tests) else pytest_style).append(f)
    return pytest_style, script_style


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

    pyt, scr = classify()

    print(f"pytest-style: {len(pyt)}   script-style: {len(scr)}\n")

    failures: list[str] = []

    if pyt:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *[str(p) for p in pyt]],
            cwd=str(ROOT),
        )
        if proc.returncode != 0:
            failures.append(f"pytest suite (exit {proc.returncode})")

    print()
    for f in scr:
        proc = subprocess.run(
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
