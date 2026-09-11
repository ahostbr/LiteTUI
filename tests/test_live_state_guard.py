"""T592 — the suite must not write the background task store the app owns.

An untracked `background-tasks.json` appeared in a worktree root carrying a
killed `t-343839 "sleep 300"` — a row from T581's arms, not from anything a
person ran. `app._save_background()` reaches `tasks.save(..., paths.ROOT)` three
frames down, so a suite run OVERWRITES the live store of anyone running LiteTUI
from that tree, and `tasks.load` marks those rows LOST at the next boot: real
in-flight work reported as gone, by a test run.

🔴 THE CARD SAID "RESOLVES RELATIVE TO CWD" AND THAT IS WRONG — IT WAS MY OWN
WORDING AND THE CARD INHERITED IT. `paths.ROOT` is derived from `__file__`, so
the store lands in the repo root from ANY working directory. The distinction
decides the fix: re-anchoring the store or chdir-ing a test changes nothing,
because cwd was never in the path. `test_the_cwd_was_never_in_the_path` pins
that so the next reader does not go hunting a bug that has never existed.

⚠️ AND IT IS WHY THE ARM THE CARD ASKED FOR WOULD HAVE BEEN A GREEN THAT PROVES
NOTHING: "run from a temp cwd, assert nothing lands there" passes identically
with the guard, without the guard, and against the unfixed code.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import paths  # noqa: E402
from litetui import tasks as tasks_mod  # noqa: E402


def _live_store() -> Path:
    return Path(paths.ROOT) / tasks_mod.STORE


def _snapshot(p: Path):
    """(exists, bytes) — compared instead of just existence, because on a real
    dev machine the live store EXISTS, and 'it is still there' is not the claim.
    The claim is that this run did not change it."""
    return (p.exists(), p.read_bytes() if p.exists() else None)


# ── the guard ───────────────────────────────────────────────────────


def test_saving_with_the_live_root_does_not_touch_the_live_store() -> None:
    before = _snapshot(_live_store())
    task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")

    tasks_mod.save([task], paths.ROOT)

    assert _snapshot(_live_store()) == before, (
        "a test wrote the repo-root background-tasks.json — this is the file "
        "that appeared untracked in the worktree"
    )


def test_the_saved_rows_are_still_readable_back() -> None:
    """The redirect must not turn persistence into a no-op: a test that saves
    and then loads has to see its own rows, or the guard would hide real
    breakage behind an empty store."""
    task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
    tasks_mod.save([task], paths.ROOT)

    back = tasks_mod.load(paths.ROOT)
    assert task.id in back, "save/load disagree once redirected"


def test_finish_does_not_write_the_live_log_tree() -> None:
    """`finish` is the OTHER writer — it puts the raw output in
    `<root>/output/tasks/<id>.log`. Guarding only `save` would leave the suite
    dropping log files into the running app's tree."""
    task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
    live_log = tasks_mod.log_path(task, paths.ROOT)

    tasks_mod.finish(task, "output body", True, paths.ROOT)

    assert not live_log.exists(), f"a test wrote {live_log}"


def test_an_explicit_root_is_still_honoured(tmp_path: Path) -> None:
    """The negative control, and the compatibility claim. Tests that already
    pass a tmp root (test_background_tasks.py, test_task_screens.py) must behave
    exactly as before — a guard that swallowed every root would make them pass
    while measuring a different directory."""
    task = tasks_mod.new_task("bash", {"command": "x"}, "c1")
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()

    tasks_mod.save([task], explicit)

    assert (explicit / tasks_mod.STORE).is_file(), "an explicit root was swallowed"


@pytest.mark.asyncio
async def test_the_app_path_is_covered_not_just_the_module(monkeypatch) -> None:
    """The call is INVISIBLE at the test site — that is why the guard is a
    choke point and not a fix at two call sites. This arm goes through the app
    method T581's arms actually call, so it fails if someone later guards
    `tasks.save` in a way `_save_background` routes around.
    """
    from litetui import app as m

    before = _snapshot(_live_store())

    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    async with a.run_test(size=(100, 30)) as pilot:
        task = tasks_mod.new_task("bash", {"command": "sleep 300"}, "c1")
        a.bg_tasks[task.id] = task
        a._save_background()
        await pilot.pause()

    assert _snapshot(_live_store()) == before


# ── the premise the card got wrong ──────────────────────────────────


def test_the_cwd_was_never_in_the_path(tmp_path, monkeypatch) -> None:
    """`paths.ROOT` is `__file__`-derived, so the store never followed cwd.

    Kept as an arm rather than a sentence in a commit body because the wrong
    premise reached a task card once already: someone reading "background
    tasks land in the repo root" will reach for a chdir fix, and this says in
    executable form that chdir changes nothing.
    """
    monkeypatch.chdir(tmp_path)

    assert Path(paths.ROOT) / tasks_mod.STORE != tmp_path / tasks_mod.STORE
    assert Path(paths.ROOT).is_absolute()
    assert os.getcwd() not in str(paths.ROOT)
