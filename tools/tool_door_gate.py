"""THE TOOL-AUTHORITY GATE — one door, counted as a STATEMENT not as a string.

BoldChip's invariant: every tool side effect goes through exactly one place,
`LiteTUI._execute_tool`, and that place is the only code that actually invokes
a dispatched tool function via `asyncio.to_thread(fn, ...)`.

🔴 WHY THIS IS NOT A GREP. The obvious gate is
`grep -c 'asyncio.to_thread(fn' src/litetui/*.py`. Measured on b604bc2, before
any refactor, that returns **2** — and the second hit is PROSE:
`ask_user_question.py:26` is a module docstring explaining
"app.py dispatches every tool with `await asyncio.to_thread(fn, args)`".
A gate that counts that comment reports two doors forever and the first person
to run it concludes one was opened. This walks the AST, so a sentence about the
door is not mistaken for the door.

⚠️ AND THE OLD app.py-SCOPED GATE IS RETIRED. It counted `_execute_tool(` in
app.py and expected 3 (one def + two call sites). The TurnEngine extraction
moves the two callers into `turn_engine.py`, so that query now reads 1 while
nothing is wrong. It stopped measuring what it was written to measure. This
file is its replacement and is package-scoped on purpose.

Exit 0 = invariant holds. Exit 1 = it does not, with what changed.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "src" / "litetui"


def _is_to_thread_call(node: ast.AST) -> bool:
    """`asyncio.to_thread(fn, ...)` — the actual invocation, dispatched arg."""
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if not (isinstance(f, ast.Attribute) and f.attr == "to_thread"):
        return False
    if not (isinstance(f.value, ast.Name) and f.value.id == "asyncio"):
        return False
    return bool(node.args) and isinstance(node.args[0], ast.Name) and node.args[0].id == "fn"


def scan() -> tuple[list[str], list[str]]:
    doors, callers = [], []
    for py in sorted(PKG.rglob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        rel = py.relative_to(PKG.parent.parent).as_posix()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if _is_to_thread_call(node):
                    doors.append(f"{rel}:{node.lineno} in {fn.name}()")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_execute_tool"
                ):
                    callers.append(f"{rel}:{node.lineno} in {fn.name}()")
    return doors, callers


def main() -> int:
    doors, callers = scan()
    print(f"doors  (asyncio.to_thread(fn, ...) as a STATEMENT): {len(doors)}")
    for d in doors:
        print(f"    {d}")
    print(f"callers (_execute_tool call sites):                 {len(callers)}")
    for c in callers:
        print(f"    {c}")

    ok = True
    if len(doors) != 1:
        print(f"\nFAIL: expected exactly 1 door, found {len(doors)}")
        ok = False
    elif "_execute_tool" not in doors[0]:
        print(f"\nFAIL: the door is not inside _execute_tool: {doors[0]}")
        ok = False
    if len(callers) != 2:
        # Not a hard failure on its own: a third dispatch path is legitimate
        # PROVIDED it reaches the door. It still needs a human to look.
        print(f"\nNOTE: {len(callers)} dispatch paths (was 2 at b604bc2).")
    print("\nOK: one door, and it is _execute_tool." if ok else "\nINVARIANT BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
