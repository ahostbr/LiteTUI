"""The path anchors, proven by RESOLUTION — not by reading the source.

A moved file's Path(__file__) re-homes data silently; the whole hazard of
giving ROOT a new home is that every store follows it. So the test asserts
where the anchors actually point, against an independently-derived truth.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import paths  # noqa: E402


def test_root_still_points_at_the_repo_root():
    # Independent derivation: app.py lives in src/litetui/, TWO levels under the
    # root. This read `.parent.parent` while the package was flat, and when the
    # move made it one level deeper the expectation silently followed app.py
    # down instead of staying at the repo root — the assertion failed for the
    # right reason, which is exactly what it was written to do.
    expected = Path(m.__file__).resolve().parent.parent.parent
    assert paths.ROOT == expected, (
        f"ROOT moved: {paths.ROOT} != {expected} — every store just re-homed"
    )
    # And the root is recognizably THE root: the stores anchor here.
    assert (paths.ROOT / "src" / "litetui" / "app.py").is_file()
    assert (paths.ROOT / "prompts").is_dir()


def test_app_defines_no_second_anchor():
    # One owner per fact, checked at the SOURCE: app.py imports the anchors
    # and never computes its own. (Runtime identity is deliberately not the
    # instrument — the suite's live-store guards rebind app.CONVO_DIR to tmp
    # paths per-test, and that protection must keep working.)
    src = Path(m.__file__).read_text(encoding="utf-8")
    # `from litetui import paths` binds the MODULE OBJECT, exactly as the old
    # `import paths` did — verified, not assumed: `litetui.paths is paths`, and
    # patching litetui.paths.CONVO_DIR is visible through app.paths. What the
    # ban below is really about is copying a VALUE out of the module, which does
    # NOT track a later patch. Both spellings are accepted here because both
    # preserve the one property that matters.
    assert ("\nimport paths\n" in src) or ("\nfrom litetui import paths\n" in src), (
        "app.py must bind the paths MODULE, not a value out of it"
    )
    # A value from-import COPIES the binding — app holding a private copy is how
    # a test's live-store guard patches one home while code reads another.
    assert "from paths import" not in src, "from-import copies the binding"
    assert "from litetui.paths import" not in src, (
        "same hazard through the package path — copies the value, not the module"
    )
    for defn in ("\nROOT = Path(", "\nCONVO_DIR = ", "\nSYSTEM_PROMPT_FILE = ",
                 "\nPROMPTS_DIR = ", "\nMEMORIES_DIR = "):
        assert defn not in src, f"app.py re-declares {defn.strip()!r} — two owners"
