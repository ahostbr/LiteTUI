"""Which script-style files need `_script_guard`, and whether they have it.

`conftest.py`'s guards are `@pytest.fixture(autouse=True)` — they cannot reach a
file that does not run under pytest. `_script_guard.py` exists to give the
script-style half the same protection by hand, and "by hand" is the part that
rots: nothing has ever checked that the files needing it import it.

🔴 THE RULE IS "CONSTRUCTS AN APP", AND IT WAS MEASURED, NOT ASSUMED. A bare
`LiteTUI()` reads the repo-root `settings.json` and can write `.convos` — both
resolve from the PACKAGE location (`paths.ROOT`, `settings.settings_path`,
each `Path(__file__).parent.parent.parent`), so in the main checkout they are
the developer's live files. That is the damage `conftest.py` records four times
over: the fleet registry (a1e8686), `.convos` (55001fa), the settings write that
reset `tool_iterations` 100 -> 48, and the dialog_style read that made a red
build look like a product defect.

⚠️ THIS ARM REPLACED A CARD BUILT ON A WRONG COUNT — MINE. I reported "10 of 14
script-style files run with no protection", derived from an IMPORT COUNT plus a
keyword scan, and carded it as exposure. Measured properly, by running all 14
with the three stores snapshotted and restored: NONE of the ten touches any of
them, in either environment (with run_all's env vars, and bare). The scan's hits
were a FakeApp attribute called `registered`, a test's own `tempfile.mkdtemp`,
and a file that only reads source TEXT. The instrument was self-checked against
a synthetic file that does write both stores, and caught it — so the negative is
a measurement rather than a blind spot.

So there is nothing to retrofit, and adding the import to ten files that
provably need none would be ceremony that also makes the rule harder to see.
What was missing is the rule itself, enforced: this file.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import run_all


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"))


def constructs_app(tree: ast.AST) -> bool:
    """A real `LiteTUI(...)` call anywhere in the file.

    🔴 AN AST CALL NODE, NEVER `"LiteTUI(" in src`. The substring matches the
    class named in a docstring, in a comment explaining the hazard, and in this
    very sentence. That is the same mistake one directory over: `run_all.py`'s
    exit check was a substring and filed a file script-style because a fake
    TRACEBACK FIXTURE quoted `raise SystemExit(...)`.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name) and fn.id == "LiteTUI":
            return True
        if isinstance(fn, ast.Attribute) and fn.attr == "LiteTUI":
            return True
    return False


def imports_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name == "_script_guard" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "_script_guard":
            return True
    return False


def test_every_script_file_that_builds_an_app_imports_the_guard():
    """The forward rule, and the one that matters: the NEXT script-style file to
    construct a `LiteTUI()` is the one this catches.

    The failure it prevents is invisible by construction — the test PASSES, it
    just passes against the developer's live settings instead of a sandbox, and
    the symptom surfaces days later as an unrelated assertion on somebody else's
    machine.
    """
    _, scripts = run_all.classify()
    missing = [
        f.name
        for f in scripts
        if constructs_app(_tree(f)) and not imports_guard(_tree(f))
    ]
    assert missing == [], (
        f"script-style file(s) construct a LiteTUI() without _script_guard: "
        f"{missing}. A bare app reads the repo-root settings.json and can write "
        f".convos — in the main checkout those are the developer's live files. "
        f"Add `import _script_guard` and call `pin_first_boot_env()` plus "
        f"`redirect_live_settings()` before constructing."
    )


def test_the_guard_is_not_carried_by_files_that_do_not_need_it():
    """The reverse rule, as a NEGATIVE CONTROL on the arm above.

    Without it, "import the guard everywhere" satisfies the forward rule and
    destroys the signal: the import would stop meaning "this file touches live
    state" and become decoration, which is how the next reader loses the ability
    to tell which files are dangerous by looking.
    """
    _, scripts = run_all.classify()
    gratuitous = [
        f.name
        for f in scripts
        if imports_guard(_tree(f)) and not constructs_app(_tree(f))
    ]
    assert gratuitous == [], (
        f"{gratuitous} import _script_guard without constructing an app. If one "
        f"of them started touching live state some other way, say so here and "
        f"widen `constructs_app` — do not leave the import unexplained."
    )


def test_the_rule_currently_partitions_the_script_half_exactly():
    """Pins today's answer so a drift is visible as a NUMBER as well as a name:
    4 files construct an app and all 4 are guarded; 10 do neither."""
    _, scripts = run_all.classify()
    builds = [f.name for f in scripts if constructs_app(_tree(f))]
    guarded = [f.name for f in scripts if imports_guard(_tree(f))]
    assert sorted(builds) == sorted(guarded)
    assert len(builds) == 4, f"expected 4 app-constructing script files, got {builds}"


def test_the_detector_sees_a_real_call_and_ignores_the_word():
    """The arm's own positive and negative control. A detector that answered
    `False` for everything would make both rules above vacuously green."""
    assert constructs_app(ast.parse("from litetui.app import LiteTUI\na = LiteTUI()\n"))
    assert constructs_app(ast.parse("import litetui.app as m\na = m.LiteTUI()\n"))
    assert not constructs_app(ast.parse('"""constructs a LiteTUI() somewhere"""\n'))
    assert not constructs_app(ast.parse("# LiteTUI() is what this would guard\nx = 1\n"))


def test_the_guard_import_detector_sees_both_spellings():
    assert imports_guard(ast.parse("import _script_guard\n"))
    assert imports_guard(ast.parse("from _script_guard import redirect_live_settings\n"))
    assert not imports_guard(ast.parse('"""mentions _script_guard in prose"""\n'))
