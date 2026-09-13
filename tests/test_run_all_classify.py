"""The runner's partition — T558-C.

`run_all.py` decides, per file, whether pytest may collect it or whether it must
be run as a bare script. BOTH mistakes are silent and neither is a crash:

  * a script-style file handed to pytest fires `sys.exit()` inside the COLLECTOR
    and reports INTERNALERROR + "no tests ran" for EVERY file, not just that one;
  * a pytest-style file run as a script imports, defines its tests, calls none
    of them, prints nothing and exits 0 — `ok` against zero assertions.

🔴 THE SECOND ONE HAD HAPPENED AND NOBODY COULD SEE IT. `has_tests` asked only
for a module-level `def test_*` (`tree.body`), so six files whose tests live in
`class Test*` were filed script-style. Measured 2026-09-10 at origin/main
`ccf0229`: `pytest --collect-only` on those six collected 83 tests, and
`python tests/test_thinking_probe.py` printed nothing and exited 0.

⚠️ SO THE ONE RUNNER MEASURED LESS THAN THE WRONG ONE. `conftest.py`'s
`collect_ignore` uses a substring check that matches an indented method, so a
plain `pytest tests/` collected all six. run_all's own docblock said "Two rules,
no gap" — the gap was exactly here.

These arms exist because the discriminator is the whole product of that file and
it has now been wrong FOUR times (its docblock records three earlier guesses).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import run_all  # noqa: E402


def parse(src: str) -> ast.AST:
    return ast.parse(src)


# ── what pytest can actually call ──────────────────────────────────────────

def test_a_class_based_file_is_pytest_style():
    """The regression. `class Test*` with `def test_*` methods is pytest's own
    default collection rule (`python_classes = Test*`), and a file shaped this
    way has nothing that runs when imported."""
    assert run_all._has_tests(
        parse("class TestThing:\n    def test_one(self):\n        assert True\n")
    )


def test_a_module_level_function_is_still_pytest_style():
    assert run_all._has_tests(parse("def test_one():\n    assert True\n"))


def test_an_async_test_counts_in_both_positions():
    assert run_all._has_tests(parse("async def test_one():\n    assert True\n"))
    assert run_all._has_tests(
        parse("class TestThing:\n    async def test_one(self):\n        assert True\n")
    )


def test_a_class_NOT_named_Test_does_not_count():
    """pytest would not collect it either, so counting it would send a file with
    nothing runnable to the collector and report a silent pass from the other
    side."""
    assert not run_all._has_tests(
        parse("class Helper:\n    def test_one(self):\n        assert True\n")
    )


def test_a_helper_named_test_something_inside_a_class_is_not_a_test_class():
    assert not run_all._has_tests(
        parse("class TestThing:\n    def helper(self):\n        pass\n")
    )


def test_a_file_with_nothing_runnable_is_not_pytest_style():
    assert not run_all._has_tests(parse("x = 1\nassert x == 1\n"))


# ── what exits, and what only looks like it ────────────────────────────────

def test_a_module_level_exit_is_detected_in_both_spellings():
    assert run_all._exits(parse("import sys\nsys.exit(0)\n"))
    assert run_all._exits(parse("raise SystemExit(1)\n"))


def test_an_exit_QUOTED_IN_A_STRING_is_not_an_exit():
    """`test_chrome_tool.py` carries `raise SystemExit(...)` inside a fake
    traceback fixture. The check was once a substring match, filed the file as
    script-style, and its nine assertions stopped running under pytest."""
    assert not run_all._exits(parse('TRACEBACK = "raise SystemExit(2)"\n'))
    assert not run_all._exits(parse('def f():\n    """calls sys.exit(0)"""\n'))


# ── the two rules together ─────────────────────────────────────────────────

def test_tests_AND_an_exit_means_script_style():
    """`test_ttyguard.py`'s shape: collecting it would fire its exit inside the
    collector and take the whole run down with INTERNALERROR."""
    tree = parse("import sys\n\ndef test_one():\n    assert True\n\nsys.exit(0)\n")
    assert run_all._has_tests(tree)
    assert run_all._exits(tree)


# ── the real tree ──────────────────────────────────────────────────────────

def test_the_six_class_based_files_are_in_the_pytest_half():
    """Named explicitly rather than counted, so a revert says WHICH files went
    dark. A count would move for a dozen innocent reasons and this one has to be
    unambiguous: these six hold 83 tests that measured nothing."""
    pyt, scr = run_all.classify()
    script_names = {f.name for f in scr}
    for name in (
        "test_input_history.py",
        "test_rpc.py",
        "test_send_resolve.py",
        "test_subagent.py",
        "test_thinking_collapse.py",
        "test_thinking_probe.py",
    ):
        assert name not in script_names, (
            f"{name} is filed script-style again — run as a bare script it "
            "imports, calls nothing and exits 0, which the runner reports as ok"
        )
    assert {f.name for f in pyt} >= {"test_subagent.py", "test_thinking_probe.py"}


def test_the_script_half_is_empty_by_design_and_the_branch_still_works(tmp_path, monkeypatch):
    """The negative control, restated for a tree where the category is gone.

    🔴 IT USED TO NAME test_ttyguard.py, AND THAT FILE MOVED. T699/T700/T702
    converted every script-style file to the pytest side and taught the
    classifier to read module-level exits as STATEMENTS rather than substrings
    — `test_ttyguard.py`'s exit lives under `if __name__ == "__main__":`, which
    pytest collects perfectly well. The arm then asserted a fact the fix had
    deliberately removed: a test defending the defect. It has been red on main
    since, and moving the file back to satisfy it would undo the fix.

    WHAT THE CONTROL WAS FOR IS STILL NEEDED. Its job was to stop "move
    everything into the pytest half" from being a cheap way to pass the arm
    above while breaking the run. Today the script half is EMPTY — 213 files
    pytest-side, 0 script-side — so the question becomes a different one: is it
    empty because no file is script-style, or because the branch that files
    them stopped working? An empty partition looks identical either way.

    So this plants a genuinely script-style file in a temp tree and requires
    the classifier to file it script-side. The emptiness is then a fact about
    the repository, not about a dead code path.

    (The emptiness ITSELF, and that the suite survived the conversion, are
    gated in test_script_checks.py::test_the_script_half_is_EMPTY_and_the_suite
    _is_still_there; the import-time hazard that made the script half necessary
    is gated by test_no_test_file_exits_or_runs_a_loop_at_IMPORT_time.)
    """
    _, scr = run_all.classify()
    assert [f.name for f in scr] == [], (
        f"the script half is no longer empty: {[f.name for f in scr]} — either a new "
        "file exits at import time, or one was filed script-style by mistake"
    )

    planted = tmp_path / "test_planted_script_style.py"
    planted.write_text(
        "import sys\n\ndef test_one():\n    assert True\n\nsys.exit(0)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    pytest_side, script_side = run_all.classify()
    assert [f.name for f in script_side] == ["test_planted_script_style.py"], (
        "a file with a module-level sys.exit was NOT filed script-side — the branch "
        "that protects the run from INTERNALERROR is dead, and the empty script half "
        "above proves nothing"
    )
    assert pytest_side == []


def test_the_partition_is_total_and_disjoint():
    """Every file the glob finds lands in exactly one half. A file in neither is
    measured by nothing at all, and nothing else in the runner would say so."""
    pyt, scr = run_all.classify()
    globbed = {f.name for f in TESTS.glob("test_*.py")}
    a = {f.name for f in pyt}
    b = {f.name for f in scr}
    assert a | b == globbed
    assert a & b == set()
