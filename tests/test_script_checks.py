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

HERE_FOR_IMPORT = Path(__file__).resolve().parent
if str(HERE_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(HERE_FOR_IMPORT))

import run_all  # the shared exit rule; tests/ is put on the path just above

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


# 🔴 ONE RULE, ONE HOME (T702). This file used to carry its own copy of the
# module-level-exit scan, beside `run_all._exits` which answered the same
# question with a different answer — that is the T694 shape, and two detectors
# that can disagree eventually do, with the one nobody is watching being the
# one that was right. The rule now lives in `run_all.module_level_hazards`,
# which `classify()` also uses, and the arms below assert they are literally
# the same function rather than merely consistent today.


def _hazards(path: Path) -> list[str]:
    """The rule, applied to a file, with the filename attached for the report."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    return [f"{path.name}:{h}" for h in run_all.module_level_hazards(tree)]


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

    ⬜ `run_all.classify()` USES THIS SAME FUNCTION (T702), so the repo runner
    and this arm can no longer disagree about which files are collectable. They
    did until T702: `_exits` walked the whole tree, so a guarded exit counted,
    and thirteen collectable files were filed as scripts — `test_ttyguard.py`
    among them, guarded since long before any of this.

    ⚠️ THIS IS AST, NOT GREP, FOR TWO REASONS. A grep counts the PROSE about a
    call as a call — this file's own docstrings name `sys.exit` repeatedly —
    and it cannot tell module level from inside a function, which is the
    distinction the whole rule rests on.
    """
    offenders = []
    for path in sorted(HERE.glob("test_*.py")):
        offenders.extend(_hazards(path))

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

    assert _hazards(bad), "the detector missed a module-level sys.exit"
    assert _hazards(good) == [], (
        "the detector flagged an exit inside a function or under a __main__ "
        "guard — it would report every healthy script in the suite"
    )


def test_the_two_detectors_are_ONE_FUNCTION() -> None:
    """🔴 NOT "consistent today" — THE SAME OBJECT.

    `run_all.classify()` decides which files the repo runner executes as
    scripts; the scan above decides which files may be collected. Those are the
    same question, and until T702 they were answered by two implementations
    that gave different answers. Asserting agreement on today's tree would pass
    the moment they drift on a file nobody has added yet; asserting identity
    cannot.
    """
    import inspect

    assert _hazards.__module__ == __name__
    src = inspect.getsource(_hazards)
    assert "run_all.module_level_hazards" in src, src
    assert run_all._exits(ast.parse("import sys\nsys.exit(0)\n")) is True
    assert run_all._exits(ast.parse('if __name__ == "__main__":\n    exit(0)\n')) is False


def test_classify_and_the_scan_agree_on_EVERY_file() -> None:
    """The behavioural half, over the real tree.

    A file belongs to the runner's script half exactly when it is hazardous to
    import OR has nothing for pytest to call. Anything else means the runner is
    executing as a script a file this scan calls clean — which is how thirteen
    files sat on the wrong side of the partition for months.
    """
    _, scripts = run_all.classify()
    script_names = {f.name for f in scripts}

    disagreements = []
    for path in sorted(HERE.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        should_be_script = bool(run_all.module_level_hazards(tree)) or not run_all._has_tests(tree)
        if should_be_script != (path.name in script_names):
            disagreements.append(
                f"{path.name}: rule says {'script' if should_be_script else 'pytest'}, "
                f"classify says {'script' if path.name in script_names else 'pytest'}"
            )

    assert disagreements == [], disagreements


def test_the_script_half_is_EMPTY_and_the_suite_is_still_there() -> None:
    """⭐ THE VALIDITY GATE FOR THE ARM ABOVE, which a `classify()` that called
    EVERYTHING a script would otherwise satisfy.

    🔴 MY FIRST VERSION OF THIS ARM ASSERTED THE WRONG FACT AND CAUGHT ME.
    It claimed `test_convos.py` was the one survivor and was there for the
    no-tests reason only. It was there for BOTH: it also ends in
    `raise SystemExit`, a spelling the T700 scan did not cover and the shared
    rule does. The arm went red on its own precondition, which is what a
    precondition is for — so `test_convos.py` was converted like the other
    twelve and the half is now empty.

    An empty script half is not an empty suite, and the second assertion is
    what separates those two readings: `classify()` must still be finding the
    two hundred-odd files it always found, just on the other side.
    """
    pytest_style, scripts = run_all.classify()
    assert scripts == [], [f.name for f in scripts]
    assert len(pytest_style) > 150, (
        f"classify() returned {len(pytest_style)} collectable files — an empty "
        f"script half means everything moved, not that everything vanished"
    )


def test_the_prune_covers_a_def_nested_in_a_module_level_block(tmp_path: Path) -> None:
    """🔴 THE ARM THAT MAKES `_flat`'s PRUNE LOAD-BEARING.

    Deleting that `continue` and running the whole suite changed NOTHING —
    `module_level_hazards` skips a top-level `def` before `_flat` is reached,
    so on today's tree the prune is unexercised and my comment claiming it was
    "the whole rule" was wrong. A line no test can kill is a line nobody knows
    is load-bearing, and the next reader simplifying this away would get a
    green suite for it.

    The case it actually covers: a `def` nested inside a module-level `try` /
    `if` / `with`. Those statements DO run at import, so the scan descends into
    them — but the function body inside still does not.
    """
    nested = tmp_path / "test_planted_nested.py"
    nested.write_text(
        "import sys\n"
        "try:\n"
        "    def helper():\n"
        "        sys.exit(1)\n"
        "except Exception:\n"
        "    pass\n",
        encoding="utf-8",
    )
    assert _hazards(nested) == [], (
        "an exit inside a def nested in a module-level try was reported as a "
        "module-level exit — importing that file runs nothing of the kind"
    )

    # The discriminator: the same block WITHOUT the def is a real hazard.
    bare = tmp_path / "test_planted_nested_bare.py"
    bare.write_text(
        "import sys\ntry:\n    sys.exit(1)\nexcept Exception:\n    pass\n",
        encoding="utf-8",
    )
    assert _hazards(bare), (
        "the scan stopped descending into module-level blocks altogether — it "
        "would now miss every exit inside a try or an if"
    )
