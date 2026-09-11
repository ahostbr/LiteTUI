"""List the tests that read the SOURCE TEXT of a module they do not own.

These are the tests that go red because of an edit to a file their author was
never editing -- the class that makes "the failing file is not one I touched"
a useless attribution. They are the honest membership of a merge guard.

WHY A SCRIPT AND NOT A GREP. The first enumeration of this set was a two-stage
substring grep (`ast.parse|rglob|iterdir|...` filtered by `src/litetui|SRC`).
It reported 11 files. Measured against the real criterion it had TWO false
positives (test_router_record.py and test_tool_context_wiring.py call
`iterdir` on their own tmp_path -- they own what they scan) and missed SIX
(test_ttyguard.py reaches the tree as `REPO / "src"`, test_footer.py and
test_kill_tree_honesty.py as `Path(<mod>.__file__).parent`, and
test_autoscroll.py / test_goal_loop.py / test_mcp_timeout_bounds.py via
`Path(<mod>.__file__).read_text()`). A grep matches the SPELLING a file used
to reach the tree; the criterion is about WHAT it reaches. Hence the AST.

Run:  python scripts/list_source_reading_tests.py [--paths]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"

# Reading a directory this test was handed is not reading someone else's source.
OWN_ROOTS = ("tmp_path", "tmp_dir", "sandbox", "monkeypatch")

READERS = ("read_text", "rglob", "glob", "walk")

# A BARE `__file__` is NOT a foreign root -- `Path(__file__).parent / "fixture"`
# stays inside tests/ and is the test's own. Foreign means one of:
#   * `<module>.__file__`  -- an Attribute, i.e. someone else's module
#   * a "src" path segment -- the source tree, reached from any root
def _is_foreign_expr(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "__file__":
            return True  # mod.__file__, never the bare name
        if isinstance(n, ast.Constant) and n.value == "src":
            return True
    return False


def _names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
    return out


def _foreign_constants(tree: ast.Module) -> set[str]:
    """Names bound to a foreign path expression, at ANY scope.

    The false-negative that made this necessary: five files build the path
    ONCE at module level (`SCREEN = Path(__file__).resolve().parents[1] /
    "src" / "litetui" / "settings_screen.py"`) and later call
    `SCREEN.read_text()`. At the call node the receiver is a bare Name and the
    root is invisible, so a check that only inspects the receiver reports a
    clean negative on the very files the rule exists for.
    """
    out: set[str] = set()
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Assign) and _is_foreign_expr(stmt.value):
            for t in stmt.targets:
                out.update(_names(t))
    return out


def reads_foreign_source(path: Path) -> str | None:
    """Return the reason this test reads source it does not own, else None."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None

    foreign = _foreign_constants(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # `open(APP_SRC)` / `io.open(APP_SRC)` -- a reader that takes the path
        # as an ARGUMENT rather than as a receiver. test_autocompact.py uses
        # this spelling and was a clean negative until it was handled.
        opener = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if opener == "open" and node.args:
            arg = node.args[0]
            if _is_foreign_expr(arg) or (_names(arg) & foreign):
                return "open() on a source path"

        if not isinstance(node.func, ast.Attribute):
            continue

        # inspect.getsource(<anything>) is always reading another module.
        if node.func.attr == "getsource":
            return "inspect.getsource"

        if node.func.attr not in READERS:
            continue

        recv = node.func.value
        seen = _names(recv)
        if seen & set(OWN_ROOTS):
            continue  # a fixture directory this test was given
        if _is_foreign_expr(recv):
            return f".{node.func.attr}() on a path rooted at another module"
        if seen & foreign:
            name = min(seen & foreign)
            return f".{node.func.attr}() on {name}, a bound source path"

    return None


def main() -> int:
    hits = []
    for path in sorted(TESTS.glob("test_*.py")):
        why = reads_foreign_source(path)
        if why:
            hits.append((path, why))

    want_paths = "--paths" in sys.argv
    for path, why in hits:
        name = f"tests/{path.name}"
        print(name if want_paths else f"{name:42} {why}")
    if not want_paths:
        print(f"\n{len(hits)} of {len(list(TESTS.glob('test_*.py')))} test files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
