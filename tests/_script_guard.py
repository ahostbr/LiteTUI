"""The live-store guard that `conftest.py` CANNOT give a script-style test.

🔴 WHY THIS FILE EXISTS. `conftest.py:73`'s `_never_write_the_live_settings` is
an `@pytest.fixture(autouse=True)`. **A conftest fixture cannot reach a test that
does not run under pytest.** The 13 script-style files run as bare
`python tests/x.py`, so they have always been outside it — and they are the same
13 that `pytest -q` cannot collect, so CI never saw the consequence either. One
population, two independent holes.

WHAT IT COST, MEASURED 2026-08-28 on main 607e635: `tests/test_input.py` and
`tests/test_ask_user_question.py` construct a real `LiteTUI()`, which loads the
REPO ROOT's `settings.json` — a gitignored file holding the developer's own
config. Ryan runs `dialog_style: "sidebar"`; both files assert the *modal* path.
Three arms on a clean `git archive` export settled it:

    export, no settings.json (the CI condition)  -> the modal assertions PASS
    export + his settings verbatim (sidebar)     -> 19/22, reproducing his box
    export + same file, dialog_style="modal"     -> 22/22, and 48/48, rc=0

So the red build was never a product defect; it was the suite reading live user
state. `conftest.py`'s own docstring already records THREE stores damaged this
way — the fleet registry (a1e8686), `.convos` (55001fa), and the settings write
that reset Ryan's `tool_iterations` 100 -> 48. This is the fourth, and the first
one the guard was structurally unable to prevent.

⭐ THE ASSERT IS THE LOAD-BEARING HALF, NOT THE REDIRECT. `test_integrations.py`
established the pattern in-tree: it repoints the maildir at :155-156 and then at
:170 asserts the repoint took. **A redirect that silently fails is the original
defect wearing a guard** — and it fails silently by construction, because the
symptom is a test that passes against the wrong file. Every helper here asserts.

`run_all.py:main()` carries the same idea as environment variables
(`LITETUI_NO_HARNESS`, `LITETUI_BACKEND`), each commented "conftest covers
pytest; this covers the script-style files". Settings could not join them:
`settings_path()` honours no env var, so it needs a redirect at import time,
which is what this module is.

USAGE — call BEFORE constructing an app, and keep the returned path if you want
to inspect what was written:

    import _script_guard
    _script_guard.redirect_live_settings()

`classify()` in run_all.py globs `test_*.py`, so this module is never collected
as a test by either runner.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def pin_first_boot_env() -> None:
    """Pin the two env knobs `run_all.py:main()` sets, so a file run ALONE
    behaves identically to the same file run by the suite.

    🔴 WITHOUT THIS, REDIRECTING SETTINGS MAKES A FILE WORSE STANDALONE, WHICH
    IS HOW I FOUND IT. A sandboxed settings dir is EMPTY, so `Settings()` comes
    up with `backend_chosen` false — and on a box with both engines installed
    that opens the first-boot ENGINE PICKER. `app.py:3080` makes
    `action_paste_text` a no-op while any `ModalScreen` is up, so seven paste
    assertions in `test_input.py` failed against a modal nobody mentioned.
    Measured: guard alone -> 15/22; guard + `LITETUI_BACKEND=lmstudio` -> 22/22.

    `run_all.py` already exports both, which is why the suite never showed this
    and a direct `python tests/test_input.py` did. **A test that is only correct
    under one runner is not guarded, it is lucky** — the runner was supplying a
    precondition the file never declared.
    """
    os.environ.setdefault("LITETUI_BACKEND", "lmstudio")
    os.environ.setdefault("LITETUI_NO_HARNESS", "1")

    assert os.environ.get("LITETUI_BACKEND"), "the backend pin did not take"
    assert os.environ.get("LITETUI_NO_HARNESS"), "the harness disable did not take"


def redirect_live_settings(prefix: str = "litetui-settings-") -> Path:
    """Point `settings.settings_path()`'s default at a fresh temp dir.

    An explicit `root=` is passed through untouched, exactly as the conftest
    fixture does, so a caller that already supplies its own root is unaffected.
    Only the `root=None` case — the live file — moves.

    Returns the sandbox directory. Raises AssertionError if the redirect did not
    take, which is the whole point of the function.
    """
    from litetui import settings as settings_mod

    sandbox = Path(tempfile.mkdtemp(prefix=prefix))
    real_settings_path = settings_mod.settings_path

    def redirected(root=None):
        if root is not None:
            return real_settings_path(root)
        return sandbox / settings_mod.SETTINGS_FILENAME

    settings_mod.settings_path = redirected

    live = REPO_ROOT / settings_mod.SETTINGS_FILENAME
    got = settings_mod.settings_path()
    assert got != live, f"the settings redirect did not take: still {got}"
    assert got.parent == sandbox, f"redirect landed somewhere unexpected: {got}"
    assert settings_mod.settings_path(REPO_ROOT) == live, (
        "an explicit root= must still be honoured; the guard broke a correct caller"
    )
    return sandbox


def redirect_convo_dir(prefix: str = "litetui-convos-") -> Path:
    """Point `paths.CONVO_DIR` at a fresh temp dir, and PROVE it moved.

    Several script-style files already assign `paths.CONVO_DIR` by hand and none
    of them assert; `55001fa` is what that costs when the assignment lands after
    something has already captured the old value.
    """
    from litetui import paths

    sandbox = Path(tempfile.mkdtemp(prefix=prefix))
    paths.CONVO_DIR = sandbox

    assert paths.CONVO_DIR == sandbox, "the .convos redirect did not take"
    assert paths.CONVO_DIR != REPO_ROOT / ".convos", (
        f"paths.CONVO_DIR still points at the live store: {paths.CONVO_DIR}"
    )
    return sandbox
