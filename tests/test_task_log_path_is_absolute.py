"""T643 — the task log path a MODEL is given must be one the read tool can open.

🔴 THE DEFECT. Under LITETUI_DATA_ROOT the background-task log is written beneath
the DATA ROOT, but both places that advertise it to the model printed a path
RELATIVE to that root — "output/tasks/t-xxx.log". The read tool resolves a
relative path against the process's working directory, which is the user's
CHECKOUT, so the model was handed a path that does not exist and got "file not
found" for a file that had been written correctly a directory tree away.

⚠️ IT ONLY BREAKS WHERE THE TWO ROOTS DIFFER, which is why it survived: with no
LITETUI_DATA_ROOT set they are the same directory and the relative path resolves
by coincidence. A test that never sets the env var can never see it.

⬜ THE FIX IS NOT "MAKE THE READ TOOL SMARTER". A tool that guessed a base for
relative paths would be guessing for every caller, not just this one. The path is
KNOWN at the point it is advertised — both sites already hold `root` — so the
honest thing is to say the whole thing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import tasks as tasks_mod


def _task(task_id: str = "t-abc123"):
    task = tasks_mod.new_task(tool="subagent", args={"prompt": "hello"}, convo_id="c1")
    task.id = task_id
    return task


def test_the_START_text_names_an_absolute_path(tmp_path):
    """🔴 What the model is told the moment a task is backgrounded."""
    text = tasks_mod.start_text(_task(), tmp_path)

    advertised = _advertised(text)
    assert Path(advertised).is_absolute(), f"not absolute: {advertised!r}"
    assert advertised == (tmp_path / "output" / "tasks" / "t-abc123.log").as_posix()


def test_the_FINISH_text_names_an_absolute_path(tmp_path):
    """🔴 And again when the result arrives, which is the one the model acts on."""
    text = tasks_mod.finish(_task(), "the output", True, tmp_path)

    advertised = _advertised(text)
    assert Path(advertised).is_absolute(), f"not absolute: {advertised!r}"
    assert Path(advertised).read_text(encoding="utf-8") == "the output"


def test_the_advertised_path_is_the_one_actually_written(tmp_path):
    """
    The arm that ties the two halves together. Advertising an absolute path that
    is absolute but WRONG would pass both arms above and still hand the model a
    missing file.
    """
    task = _task()
    tasks_mod.finish(task, "body", True, tmp_path)

    assert tasks_mod.log_path(task, tmp_path).exists()
    assert Path(_advertised(tasks_mod.start_text(task, tmp_path))) == tasks_mod.log_path(
        task, tmp_path
    )


def test_a_DATA_ROOT_that_differs_from_the_cwd_is_the_whole_point(tmp_path, monkeypatch):
    """
    🔴 THE INCIDENT'S OWN SHAPE. Sitting in one directory while the root is
    another is the only configuration where the old code was wrong, so the arm
    puts the process somewhere else on purpose. Resolving the advertised path
    against the CWD must still find the file.
    """
    elsewhere = tmp_path / "checkout"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    root = tmp_path / "data-root"
    task = _task()
    tasks_mod.finish(task, "found me", True, root)
    advertised = _advertised(tasks_mod.start_text(task, root))

    # `Path.resolve()` is what a reader does with a relative path; the old
    # relative form resolved to <elsewhere>/output/tasks/... and did not exist.
    assert Path(advertised).resolve().read_text(encoding="utf-8") == "found me"


def test_CONTROL_a_log_that_cannot_be_written_says_so_instead_of_a_path(tmp_path):
    """
    The existing behaviour, kept: an unwritable log reports the failure rather
    than advertising a path to nothing.
    """
    task = _task()
    blocker = tmp_path / "output"
    blocker.write_text("I am a file where a directory needs to be", encoding="utf-8")

    text = tasks_mod.finish(task, "body", True, tmp_path)

    assert "could not be written" in text
    assert "output/tasks" not in text


def _advertised(text: str) -> str:
    """The path out of an advertisement, however it is worded."""
    for token in text.replace("\n", " ").split(" "):
        if token.endswith(".log"):
            return token.strip().rstrip(".,;")
    raise AssertionError(f"no .log path advertised in: {text!r}")
