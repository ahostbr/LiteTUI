"""Counterexamples for run_all.classify() branches the existing arms don't reach.

`test_run_all_classify.py` and `test_script_checks.py` already pin the two
_has_tests shapes (module `def test_*`, `class Test*`) and every `_exits`
spelling over the real tree. What they do NOT exercise is classify() itself on
three tiny synthetic classes, so this file plants those and asserts the
partition directly. classify() is a pure AST partition — it globs a directory,
reads and parses each file, and sorts it into a half; it runs NO pytest and NO
script — so `monkeypatch`-ing `run_all.TESTS` to a temp dir and calling it is
safe and does not collect or execute anything.

Scope note (approved): these REPORT the classifier's current behavior on the
edge classes; none proposes a runner change. Where the behavior is a known
limitation it is named as one, not asserted as correct.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

import run_all  # noqa: E402


def _classify(tmp_path, monkeypatch, files: dict[str, str]) -> tuple[set[str], set[str]]:
    """Write `files` (name -> source) into a temp tree and run classify() there.

    No pytest, no subprocess — classify() only reads and AST-parses.
    """
    for name, src in files.items():
        (tmp_path / name).write_text(src, encoding="utf-8")
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    pyt, scr = run_all.classify()
    return {f.name for f in pyt}, {f.name for f in scr}


# ── G1: malformed syntax — must NOT be filed pytest-style ───────────────────

@pytest.mark.parametrize("bad_src", [
    "def test_x(:\n    assert True\n",              # plain syntax error, no exit text
    "def test_x(:\n    sys.exit(0)\n",              # syntax error AND an exit substring
], ids=["no-exit-text", "with-exit-substring"])
def test_a_malformed_file_is_filed_script_side_not_pytest(tmp_path, monkeypatch, bad_src):
    """classify()'s `except SyntaxError` fallback is exercised by nothing today:
    the other files ast.parse in the test (which raises) or skip SyntaxError
    files entirely. If a regression let a malformed file fall into the pytest
    half, pytest would hit the SyntaxError during COLLECTION and abort the whole
    run — the exact INTERNALERROR class the partition exists to prevent. So a
    malformed file must land script-side, and it does so regardless of whether
    it happens to contain an exit substring (has_tests is forced False on the
    parse failure, which is sufficient on its own)."""
    pyt, scr = _classify(tmp_path, monkeypatch, {
        "test_broken.py": bad_src,
        "test_good.py": "def test_ok():\n    assert True\n",   # positive control
    })
    assert "test_broken.py" in scr
    assert "test_broken.py" not in pyt
    assert "test_good.py" in pyt          # discrimination: a valid file still collects


# ── G2: helper-only — no test_, no exit ─────────────────────────────────────

def test_a_helper_only_file_is_filed_script_side(tmp_path, monkeypatch):
    """A `test_*.py` with helpers but no `def test_*`/`class Test*` and no
    module-level exit has `not has_tests` True, so classify() files it
    script-side (the arm the only existing planted case — which carries a
    sys.exit — never reaches). Run as a bare script it imports, calls nothing
    and exits 0: a silent pass over zero assertions, which is exactly why the
    partition sends it to the script half rather than letting pytest report a
    vacuous green."""
    pyt, scr = _classify(tmp_path, monkeypatch, {
        "test_helper_only.py": "def build_fixture():\n    return {'k': 1}\n",
    })
    assert "test_helper_only.py" in scr
    assert "test_helper_only.py" not in pyt


# ── G3: duplicate test-function names — a NAMED limitation ──────────────────

def test_duplicate_test_names_are_pytest_style_and_the_shadow_is_uncertified(tmp_path, monkeypatch):
    """DOCUMENTED LIMITATION, not a defect. classify() answers 'can pytest
    collect this file' — it returns pytest-style on the FIRST `def test_*` and
    makes NO claim about how many distinct tests survive. Two `def test_dup`
    in one module is legal Python: the second binding shadows the first, pytest
    collects ONE, and the runner's partition neither detects nor reports the
    lost test. Classification certifies collectABILITY, never collection COUNT.
    This arm pins that contract so a future reader does not mistake 'pytest-side'
    for 'every test here runs'."""
    pyt, scr = _classify(tmp_path, monkeypatch, {
        "test_dupes.py": (
            "def test_dup():\n    assert True\n\n"
            "def test_dup():\n    assert False\n"      # shadows the first, silently
        ),
    })
    assert "test_dupes.py" in pyt
    assert "test_dupes.py" not in scr


# ── Conditional def — reported behavior, not a runner change ─────────────────

def test_a_conditionally_defined_test_is_unseen_and_filed_script_side(tmp_path, monkeypatch):
    """REPORTED LIMITATION. `_has_tests` scans only top-level `tree.body`, so a
    `def test_*` nested under a module-level `if` is not seen — even though
    pytest WOULD collect it once the guard runs at import. classify() therefore
    files such a file script-side, where a bare-script run defines the function
    and calls nothing. This documents the current AST-body-only behavior; per
    scope it is reported here, not fixed in the runner."""
    src = "if True:\n    def test_conditional():\n        assert True\n"
    assert run_all._has_tests(ast.parse(src)) is False   # the mechanism, directly
    assert run_all._exits(ast.parse(src)) is False
    pyt, scr = _classify(tmp_path, monkeypatch, {"test_conditional.py": src})
    assert "test_conditional.py" in scr
    assert "test_conditional.py" not in pyt


# ── Meta: this inventory file must itself be pytest-style ────────────────────

def test_this_inventory_file_is_itself_pytest_style():
    """If this file ever grew a module-level exit or lost its `def test_*`,
    classify() would file IT script-side — run as a bare script it would import,
    call none of these arms and exit 0, and every counterexample above would
    stop protecting the classifier while still reporting green. Guard that by
    reading this file's own source through the same two rules."""
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    assert run_all._has_tests(tree) is True
    assert run_all._exits(tree) is False
