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

import ast
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
#: The two T699 converted; T700 did ten more. Named here because these are the
#: ones this file drives END TO END as scripts — the whole class is covered by
#: the AST scan at the bottom, which needs no list at all.
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


# ── the class cannot grow back (T700) ───────────────────────────────────


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
#: Calls that end or hijack the interpreter when they run at import.
_EXITERS = {("sys", "exit"), ("os", "exit"), ("os", "_exit"), ("", "exit"),
            ("", "quit"), ("asyncio", "run")}


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

    🔴 THE PRUNE IS THE WHOLE DETECTOR. A plain `ast.walk` over each top-level
    statement descends into every `def` in the file, so `asyncio.run` inside a
    helper reads exactly like one at module level. My first cut did that and
    flagged 28 files where 10 are real — and a detector that cries wolf on 18
    innocents is one somebody switches off.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFS):
            continue
        yield from _flat(child)


def _module_level_exits(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    hits = []
    for stmt in tree.body:
        if isinstance(stmt, _DEFS) or _is_main_guard(stmt):
            continue
        for node in _flat(stmt):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = getattr(getattr(func, "value", None), "id", "")
            if (owner, name) in _EXITERS:
                hits.append(f"{path.name}:{node.lineno} {owner + '.' if owner else ''}{name}")
    return hits


def test_no_test_file_exits_or_runs_a_loop_at_IMPORT_time() -> None:
    """🔴 THE CLASS, NOT THE TEN MEMBERS.

    A `sys.exit` or an `asyncio.run` at module level in a `test_*.py` runs
    during COLLECTION. SystemExit there is reported as INTERNALERROR and
    abandons the whole invocation — every other file in it included.

        THE FAILURE HIDES ITS OWN POPULATION. A run stops at the FIRST
        offender, so each abort conceals the rest: the orchestrator's gate
        found three members, and an AST scan of the same directory found TEN.

    ⬜ AN EXIT UNDER `if __name__ == "__main__":` IS FINE and is what the ten
    conversions did — these files are still useful as hand-run scripts, and
    that is the one place the exit belongs.

    ⚠️ THIS AND `run_all._exits` DISAGREE ON PURPOSE, AND NEITHER IS BROKEN.
    `run_all.classify` asks "should this file be RUN as a script?" and walks the
    whole tree, so a guarded exit still counts; this asks "does importing it end
    the interpreter?" and exempts the guard. The consequence is measured and is
    its own card: with the guard exempted, the script half falls from 14 files
    to 1, and `test_ttyguard.py` — which has guarded its exit all along — has
    been on the wrong side of that partition the whole time. Not changed here;
    rewriting how the repo runner executes the suite is not a test-file fix.

    ⚠️ THIS IS AST, NOT GREP, FOR TWO REASONS. A grep counts the PROSE about a
    call as a call — this file's own docstrings name `sys.exit` repeatedly —
    and it cannot tell module level from inside a function, which is the
    distinction the whole rule rests on.
    """
    offenders = []
    for path in sorted(HERE.glob("test_*.py")):
        offenders.extend(_module_level_exits(path))

    assert offenders == [], (
        "these files exit or start an event loop at import, so collecting any "
        "of them aborts the entire pytest run:\n  "
        + "\n  ".join(offenders)
        + "\nPut the call behind `if __name__ == \"__main__\":` and expose the "
          "checks as a test function — see tests/test_store.py."
    )


def test_the_detector_CATCHES_a_planted_offender(tmp_path: Path) -> None:
    """⭐ THE POSITIVE CONTROL, and this detector needs one more than most.

    It asserts an EMPTY list, so it is green when it works and green when it is
    broken — a prune that pruned everything, a name set that matched nothing, a
    parse that silently returned []. Three plausible mistakes, all invisible.
    Two files are planted: one that offends and one that does the same thing
    correctly, so the control proves the detector DISCRIMINATES rather than
    merely fires.
    """
    bad = tmp_path / "test_planted_bad.py"
    bad.write_text("import sys\nok = [1]\nsys.exit(0)\n", encoding="utf-8")
    good = tmp_path / "test_planted_good.py"
    good.write_text(
        "import sys\n\n\ndef helper():\n    sys.exit(1)\n\n\n"
        "if __name__ == \"__main__\":\n    sys.exit(0)\n",
        encoding="utf-8",
    )

    assert _module_level_exits(bad), "the detector missed a module-level sys.exit"
    assert _module_level_exits(good) == [], (
        "the detector flagged an exit inside a function or under a __main__ "
        "guard — it would report every healthy script in the suite"
    )
