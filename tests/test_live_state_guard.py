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
import re
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


# ── what an autouse fixture may leave behind ────────────────────────

# 🔴 THE GUARD ABOVE HAD A COST NOBODY MEASURED, AND IT SHIPPED (T596).
# `_never_write_the_live_task_store` (conftest.py) redirects the live root into
# `tmp_path`, and its first version created the destination EAGERLY — `e31e866`.
# Being autouse, that put a `task-store` directory into EVERY test's tmp_path,
# including tests that never touch the store. `test_router_record.py`'s
# `test_the_write_is_atomic_and_leaves_no_tmp` asserts the write leaves exactly
# `["router.json"]` and got `["router.json", "task-store"]`; `77e03de` moved the
# mkdir inside `_swap` so it happens ON REDIRECT.
#
#     A FIXTURE THAT RUNS FOR EVERY TEST CHANGES THE ENVIRONMENT OF EVERY TEST,
#     INCLUDING THE ONES IT HAS NOTHING TO DO WITH.
#
# ⚠️ AND THE FAILURE LANDED ON A STRANGER. The arm that went red belongs to the
# router, not to tasks — so the report named a file whose author had changed
# nothing, which is the most expensive way for this class of defect to be found.
# Nothing in the tasks guard's own arms could have caught it: they all touch the
# store, so they all take the redirect, so tmp_path is legitimately non-empty in
# every one of them. The arm has to be a test that touches NOTHING.


def _conftest_source() -> str:
    return (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")


def _seeded_by(fixture: str) -> set[str]:
    """The tmp_path entries this autouse fixture NAMES, read from its source.

    ⬜ PARSED, NOT RETYPED, and that is the whole discipline of the exemption
    below. A hand-written allow-list is a second copy of a fact the fixture
    already states, and the two would drift the first time the directory was
    renamed — leaving an arm that passes while guarding a name nothing creates.

    ⚠️ IT READS NAMES, NOT CREATIONS, AND THE NAME OF THIS FUNCTION OVERSTATED
    THAT UNTIL THIS LINE. `_never_write_the_live_task_store` MENTIONS
    `tmp_path / "task-store"` at its body level and creates it only inside
    `_swap`; this parser cannot tell those apart and does not try. It does not
    need to: exemption is a deliberate act by a person, and what the parse
    supplies is only the SPELLING of the entry, so the two cannot drift. What
    decides whether an entry is allowed is EXEMPT.

    ⚠️ LITERALS ONLY. `_never_write_the_live_settings` builds its path from
    `settings_mod.SETTINGS_FILENAME`, a constant this cannot resolve, so
    exempting that fixture would grant NOTHING and the arm would stay red. That
    is the safe direction — an exemption that cannot be read grants nothing —
    but it is stated here so the next reader debugs the parser rather than the
    exemption when it happens.
    """
    src = _conftest_source()
    body = src[src.index(f"def {fixture}(") :]
    end = body.find("\n@pytest.fixture")
    if end != -1:
        body = body[:end]
    names: set[str] = set()
    for line in body.splitlines():
        bare = line.split("#", 1)[0]
        m = re.search(r'tmp_path\s*/\s*"([^"]+)"', bare)
        if m:
            names.add(m.group(1))
    return names


# 🔴 EMPTY ON PURPOSE, AND IT IS THE ASSERTION. No autouse fixture in this suite
# is entitled to seed a directory today — both of them create on demand. A
# fixture that genuinely needs to (a cache the app cannot create itself, say)
# gets its NAME added here as a deliberate act, and the entry it is allowed to
# leave is then read out of its source by `_seeded_by` rather than written twice.
EXEMPT: tuple[str, ...] = ()


def test_a_test_that_touches_nothing_leaves_tmp_path_empty(tmp_path: Path) -> None:
    """The arm `e31e866` needed and did not have.

    It asks for nothing, does nothing, and asserts the autouse fixtures left it
    that way. Every other test in this file takes the redirect deliberately, so
    none of them could ever have noticed.
    """
    allowed = {name for fixture in EXEMPT for name in _seeded_by(fixture)}
    left = sorted(entry.name for entry in tmp_path.iterdir())

    assert [name for name in left if name not in allowed] == [], (
        f"an autouse fixture seeded {left} into a test that touched nothing. "
        f"Create on demand instead (see `_swap` in conftest.py), or add the "
        f"fixture to EXEMPT above if it genuinely must seed. Allowed today: "
        f"{sorted(allowed) or 'nothing'}"
    )


def test_the_exemption_is_read_from_the_fixture_and_not_retyped() -> None:
    """⭐ POSITIVE CONTROL FOR THE PARSER, because an EXEMPT tuple that is empty
    today makes `_seeded_by` unreachable — and an unreachable helper is one
    nobody finds out is broken until the day it is needed, which is the day
    somebody is already debugging something else.

    Driven against the real fixture: it DOES name `tmp_path / "task-store"`, in
    the lazy branch. So the parser can see the name; what makes the arm above
    pass is that the fixture is not in EXEMPT, not that the name is invisible.
    """
    assert _seeded_by("_never_write_the_live_task_store") == {"task-store"}


def test_CONTROL_the_parser_reads_nothing_from_a_fixture_that_names_nothing() -> None:
    """The other half: a name that is not there must not be invented. Without
    this, `_seeded_by` returning a constant would satisfy the arm above."""
    assert _seeded_by("_never_dial_out_from_a_constructor") == set()


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
