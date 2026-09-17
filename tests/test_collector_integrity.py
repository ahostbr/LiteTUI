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
        # NO SKIP ANY MORE, AND THAT IS STRICTER (T838). This used to pass
        # over files conftest would not import. conftest ignores nothing
        # now, so pytest imports EVERY test_*.py and every one of them has
        # to be safe to import. Deleting the net widened this guard.
        hit = MODULE_LEVEL_CALL.search(path.read_text(encoding="utf-8", errors="ignore"))
        if hit:
            offenders.append(f"{path.name}: module-level {hit.group(0)!r}")

    assert not offenders, (
        "These files define `def test_` (so pytest imports them) AND run "
        "themselves at import. Importing one aborts collection for the whole "
        "repo:\n  " + "\n  ".join(offenders) +
        "\nMove the module-level call under `if __name__ == \"__main__\":`. "
        "Dropping `def test_` is NO LONGER a way out: conftest ignores "
        "nothing since T838, so an uncollectable file is still imported."
    )
