"""THE GUARD THAT OUTLIVES ITS SUBJECT — tests/test_tool_cancel.py kept honest
from OUTSIDE the file it is about.

🔴 WHY THIS IS NOT A TEST INSIDE THAT FILE. A guard living in the file at risk
cannot detect that file being reverted: one "accept ours" resolution deletes the
guard AND its subject in the same move, and the suite goes green — the guard
does not FAIL, it CEASES TO EXIST, which is indistinguishable from passing.

The concrete hazard this was written for (2026-08-23): `fix/kill-tree-honesty`
cherry-picked `c870f05` to get a working instrument, then this file's branch
moved twice — `ad44464` rewrote the tree-kill gate to walk the AST instead of
matching source text, and `7b50a5a` added the probe reaper. A divergent copy of
one file resulted, no merge ORDER could reconcile it, and the tempting
resolution silently reverts both improvements while every test still passes,
because the OLD gate passes on good input. That is the exact defect `ad44464`
fixed.

⚠️ AND GREP CANNOT SEE IT. Both versions of `test_the_kill_is_a_tree_kill`
contain the string "/T" — that is the entire point of the AST rewrite. Anyone
checking for the reversion with grep finds nothing wrong and concludes it is
fine. The tells are structural, so this gate is structural.

What it asserts, and nothing wider:
  1. `test_the_kill_is_a_tree_kill` parses source into an AST rather than
     matching text — i.e. it calls `ast.parse`.
  2. `_reap` exists and is called from the `finally` of at least three test
     arms, so a probe cannot be left running when an arm dies mid-way.

Standalone and stdlib only ON PURPOSE: it must run from a clean checkout of
main. It deliberately does NOT import tools/tool_door_gate.py, which lives only
on an unmerged branch — a guard that cannot run until an unrelated branch lands
is its own version of this bug.

Exit 0 = both invariants hold. Exit 1 = they do not, naming what changed.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

TESTFILE = (Path(__file__).resolve().parent.parent
            / "tests" / "test_tool_cancel.py")
GATE_TEST = "test_the_kill_is_a_tree_kill"
REAPER = "_reap"
MIN_REAPING_ARMS = 3


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def _calls_ast_parse(fn: ast.AST) -> bool:
    """Does this function turn source into a TREE, rather than matching text?"""
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "parse"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ast"):
            return True
    return False


def _reaps_in_finally(fn: ast.AST) -> bool:
    """Is the reaper called from this arm's `finally`?

    The finally is the point: a reap on the happy path only cleans up after
    tests that did not need it. The leak happens when an arm DIES.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        for stmt in node.finalbody:
            for call in ast.walk(stmt):
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == REAPER):
                    return True
    return False


def main() -> int:
    if not TESTFILE.exists():
        _fail(f"{TESTFILE} is gone")
        return 1
    try:
        tree = ast.parse(TESTFILE.read_text(encoding="utf-8"))
    except SyntaxError as e:
        _fail(f"{TESTFILE.name} does not parse: {e}")
        return 1

    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    ok = True

    # 1. the tree-kill gate counts a STATEMENT, not a string
    gate = funcs.get(GATE_TEST)
    if gate is None:
        _fail(f"{GATE_TEST} is gone from {TESTFILE.name}")
        ok = False
    elif not _calls_ast_parse(gate):
        _fail(f"{GATE_TEST} no longer parses source into an AST — it is back to "
              f"matching text, which passes with /T REMOVED as long as a "
              f"comment mentions it (that is why ad44464 exists)")
        ok = False
    else:
        print(f"  [ok] {GATE_TEST} walks the AST")

    # 2. the probe reaper still runs when an arm dies
    if REAPER not in funcs:
        _fail(f"{REAPER}() is gone — a test that dies mid-way leaves its probe "
              f"running, inflating the process table every other measurement "
              f"on this box is taken against")
        ok = False
    else:
        arms = sorted(name for name, fn in funcs.items()
                      if name.startswith("test_") and _reaps_in_finally(fn))
        if len(arms) < MIN_REAPING_ARMS:
            _fail(f"only {len(arms)} arm(s) reap in a finally, expected at least "
                  f"{MIN_REAPING_ARMS}: {arms}")
            ok = False
        else:
            print(f"  [ok] {REAPER}() called from the finally of {len(arms)} arms")

    if not ok:
        print("\n  The likely cause is a merge resolution that took the older "
              "copy of this file.\n  Compare: git rev-parse "
              "main:tests/test_tool_cancel.py")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
