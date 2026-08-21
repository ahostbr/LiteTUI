"""The suite must stay reachable from `pytest`.

On 2026-08-21 `python -m pytest tests/` reported an INTERNALERROR and
**zero tests collected** for the whole repository. One file ended in a bare
`asyncio.run(main())` with no `__main__` guard, so pytest's own collector
imported it, ran it, and took a `SystemExit` to the face. Thirteen files were
in that state; ignoring one only handed the crash to the next.

Nothing noticed, for the same reason it is worth a test now: **the scripts were
all passing.** They run standalone, print `N/M passed`, and exit 0. The signal
everyone looked at was green, and the entry point nobody typed was dead.

conftest.py keeps the two kinds apart -- a pytest module defines `def test_`, a
script does not, and scripts are never collected. This file guards the edge that
rule cannot see on its own: a file that is BOTH. Add `def test_` to a script, or
a module-level `asyncio.run(main())` to a real test module, and pytest will
import it, run it, and die in collection again.

⚠️ THIS TEST CANNOT CATCH ITSELF FAILING TO RUN. If collection breaks, this file
never executes either -- a green run proves collection worked, and a broken
collector produces no report at all rather than a red one. That is exactly the
failure shape it exists for, so read `no tests collected` as a FAILURE, never as
a suite that had nothing to say.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: A call at column 0 -- i.e. executed on import, not inside a function or a
#: `if __name__ == "__main__"` block. Indented calls are fine by construction.
MODULE_LEVEL_CALL = re.compile(
    r"^(?:asyncio\.run\(|main\(\)|sys\.exit\()", re.MULTILINE
)


def _files() -> list[Path]:
    return sorted(TESTS.glob("test_*.py"))


def _is_collected(path: Path) -> bool:
    """Mirrors conftest.collect_ignore: no `def test_` means pytest skips it."""
    return "def test_" in path.read_text(encoding="utf-8", errors="ignore")


def test_no_collected_file_runs_itself_on_import():
    """A file pytest imports must not execute or exit at module level.

    This is the exact defect: `sys.exit()` during collection is an
    INTERNALERROR, and an INTERNALERROR collects nothing at all -- not the
    offending file, the ENTIRE repository.
    """
    offenders = []
    for path in _files():
        if path.name == Path(__file__).name:
            continue
        if not _is_collected(path):
            continue  # a script, and conftest never imports it
        hit = MODULE_LEVEL_CALL.search(path.read_text(encoding="utf-8", errors="ignore"))
        if hit:
            offenders.append(f"{path.name}: module-level {hit.group(0)!r}")

    assert not offenders, (
        "These files define `def test_` (so pytest imports them) AND run "
        "themselves at import. Importing one aborts collection for the whole "
        "repo:\n  " + "\n  ".join(offenders) +
        "\nEither move the module-level call under `if __name__ == \"__main__\":`, "
        "or drop `def test_` so conftest treats the file as a script."
    )


def test_every_test_file_is_classified_and_the_split_is_real():
    """Both kinds must be non-empty, or the rule has quietly stopped working.

    If `collected` ever hits zero the suite is dead and every other test in it
    is vacuously silent -- the 2026-08-21 state. If `scripts` hits zero the
    conftest rule has stopped recognising them, which is how they get imported
    and the crash returns.
    """
    files = _files()
    collected = [p.name for p in files if _is_collected(p)]
    scripts = [p.name for p in files if not _is_collected(p)]

    assert len(collected) + len(scripts) == len(files)
    assert collected, "pytest collects NOTHING -- the suite is unreachable"
    assert scripts, "no script-style files found; the conftest rule matches nothing"
    # Print the population rather than asserting a magic number: a fixture that
    # asserts a state is empty when the state holds is how a pass gets reported
    # by code that never ran.
    print(f"\n{len(collected)} pytest modules collected, {len(scripts)} scripts skipped")


def test_conftest_actually_ignores_the_scripts():
    """The rule must be WIRED, not merely written.

    conftest could define `collect_ignore` and still not populate it -- a
    remedy that exists and is never invoked looks identical to one that works,
    right up until the collector dies again.
    """
    import conftest

    ignored = set(getattr(conftest, "collect_ignore", []))
    expected = {p.name for p in _files() if not _is_collected(p)}
    assert ignored == expected, (
        f"conftest.collect_ignore is out of step with the files on disk.\n"
        f"  ignored but shouldn't be: {sorted(ignored - expected)}\n"
        f"  should be ignored but isn't: {sorted(expected - ignored)}"
    )
