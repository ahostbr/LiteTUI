"""Skills must be visible to the HUMAN, not just to the model.

`discover()` skips a directory with no SKILL.md silently, and that is correct —
a scratch folder is not a broken skill. But it means a MISNAMED or MISPLACED
skill is indistinguishable from one that was never written, which is the exact
failure skills.py's own docstring warns about for the model ("a capability with
no pointer is indistinguishable from an absent one"). The human had the same
blind spot: nothing listed what had been found, or what had been passed over.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

import app as app_mod
import paths
import skills as skills_mod

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-skills-"))


#: The real ROOT, captured before any test moves it.
_REAL_ROOT = paths.ROOT


@pytest.fixture(autouse=True)
def _restore_root():
    """ROOT is a MODULE GLOBAL. Mutating it leaks into every other test module
    in the same pytest process — the suite ran fine alone and collapsed when run
    together. Always put it back."""
    yield
    paths.ROOT = _REAL_ROOT


def _app_with(tmp_root: Path):
    paths.ROOT = tmp_root
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    return a


def _make_skill(base: Path, folder: str, name: str, desc: str) -> None:
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )


def test_disabled_skills_yield_a_list_not_a_dict():
    """load() iterates its argument; a dict would yield KEYS, not Skill objects.

    Latent rather than live — load() is only reachable when self.skills is
    truthy — but the two branches must agree on type or the bug waits.
    """
    from settings import Settings

    a = app_mod.LiteTUI()
    a.settings = Settings(skills_enabled=False)
    a.skills = skills_mod.discover(paths.ROOT) if a.settings.skills_enabled else []
    assert isinstance(a.skills, list)


@pytest.mark.asyncio
async def test_slash_skills_names_what_was_SKIPPED():
    """The whole point. A folder without SKILL.md must be REPORTED, not hidden.

    This is the discriminating case: discovery already worked for valid skills,
    and the failure being fixed is invisible skipping.
    """
    root = Path(tempfile.mkdtemp(prefix="skills-root-"))
    base = root / "skills"
    _make_skill(base, "good-one", "good-one", "a real skill")
    (base / "typo-folder").mkdir(parents=True)          # no SKILL.md
    (base / "another-mistake").mkdir(parents=True)      # no SKILL.md

    a = _app_with(root)
    msgs: list[str] = []
    pushed: list = []
    async with a.run_test() as pilot:
        await pilot.pause()
        a._system = lambda s: msgs.append(s)
        a.push_screen = lambda screen, cb=None: pushed.append((screen, cb))
        a._handle_command("/skills")
        await pilot.pause()

    # THE POINT OF THIS TEST IS UNCHANGED: a folder that produced no skill must
    # be REPORTED, not silently passed over. It is still said out loud, because
    # a picker cannot show a folder that has no row.
    out = "\n".join(msgs)
    assert "typo-folder" in out, "a SKIPPED folder was not reported — the bug"
    assert "another-mistake" in out
    assert "no SKILL.md" in out, "skipped folders were listed without saying why"

    # The LISTING moved into a picker (2026-08-22) — /skills answered "what do
    # I have" with ~80 lines of transcript. So the loaded skill is asserted on
    # its new surface rather than dropped from the test.
    assert pushed, "/skills no longer opens a picker"
    ids = [row_id for row_id, _label in pushed[0][0]._rows]
    assert "good-one" in ids, "the loaded skill was not listed in the picker"


@pytest.mark.asyncio
async def test_slash_skills_actually_gives_the_body_to_the_model():
    """/skills <name> must SEND the body, not print it.

    AMENDED 2026-08-22, and the old name is the evidence: this was
    `test_slash_skills_shows_what_the_MODEL_would_get`, and it asserted the body
    appeared in the CHAT LOG while calling that "what the model would get".
    Those are two different places and the command only ever did the first.
    Ryan: "invoking a skill just prints it to the screen... its not getting sent
    to the agent correctly."

    The test passed for as long as the bug existed BECAUSE its expectation was
    read off the implementation instead of the contract. Left here, renamed,
    rather than deleted — a test that once encoded a defect is worth keeping as
    a reminder that green meant nothing here.
    """
    root = Path(tempfile.mkdtemp(prefix="skills-root2-"))
    _make_skill(root / "skills", "probe", "probe", "d")
    (root / "skills" / "probe" / "SKILL.md").write_text(
        "---\nname: probe\ndescription: d\n---\n\nUNIQUE-BODY-MARKER-42\n", encoding="utf-8"
    )

    a = _app_with(root)
    msgs: list[str] = []
    bubbles: list[str] = []
    turns: list[int] = []
    async with a.run_test() as pilot:
        await pilot.pause()
        a._system = lambda s: msgs.append(s)
        a._user_bubble = lambda t, h, queued=False: bubbles.append(t)
        a._stream = lambda: turns.append(1)
        a._handle_command("/skills probe")
        await pilot.pause()
    convo = "\n".join(str(m.get("content") or "") for m in a.conversation)
    assert "UNIQUE-BODY-MARKER-42" in convo, (
        "the body never reached the conversation the model will be sent"
    )
    assert turns == [1], "the body was delivered but no turn was started"
    assert "UNIQUE-BODY-MARKER-42" not in "\n".join(msgs + bubbles), (
        "the body is still being dumped onto the screen"
    )
    # The user is told on the BUBBLE now, not by a system line — a system line
    # beside the bubble would be the printing Ryan asked to remove, by another
    # name.
    assert any("probe" in b for b in bubbles), (
        f"nothing on screen names the loaded skill; bubbles={bubbles!r}"
    )


@pytest.mark.asyncio
async def test_an_empty_directory_says_what_it_expects():
    """"0 skills" with no explanation sends you to read the source."""
    root = Path(tempfile.mkdtemp(prefix="skills-root3-"))
    (root / "skills").mkdir(parents=True)

    a = _app_with(root)
    msgs: list[str] = []
    pushed: list = []
    async with a.run_test() as pilot:
        await pilot.pause()
        a._system = lambda s: msgs.append(s)
        a.push_screen = lambda screen, cb=None: pushed.append((screen, cb))
        a._handle_command("/skills")
        await pilot.pause()
        # The explanation now lives in the full report, which is the picker's
        # first row rather than the default wall of text. Still one keystroke
        # away, which is what this test has always been about: "0 skills" with
        # no explanation sends you to read the source.
        assert pushed, "/skills no longer opens a picker"
        pushed[0][1](pushed[0][0]._rows[0][0])
        await pilot.pause()
    out = "\n".join(msgs)
    assert "SKILL.md" in out, "an empty directory did not say what it wants"


@pytest.mark.asyncio
async def test_disabled_says_so_rather_than_reporting_zero():
    """"0 skills" and "skills are off" are different facts.

    Reporting the first when the second is true sends you hunting for a missing
    file that is not missing.
    """
    from settings import Settings

    root = Path(tempfile.mkdtemp(prefix="skills-root4-"))
    _make_skill(root / "skills", "present", "present", "d")

    a = _app_with(root)
    a.settings = Settings(skills_enabled=False)
    a.skills = []
    msgs: list[str] = []
    async with a.run_test() as pilot:
        await pilot.pause()
        a._system = lambda s: msgs.append(s)
        a._handle_command("/skills")
        await pilot.pause()
    assert "OFF" in msgs[-1] or "off" in msgs[-1]


def test_the_shipped_example_skill_is_discoverable():
    """The repo's own skills/ must actually parse — a broken example is worse
    than none, because it is what the next skill gets copied from."""
    # The repo root, not tests/ — this file moved and the skills/ it checks did not.
    repo = Path(__file__).resolve().parent.parent
    found = skills_mod.discover(repo)
    assert found, "the shipped skills/ directory discovered nothing"
    names = {s.name for s in found}
    assert "look-at-the-screen" in names
    one = next(s for s in found if s.name == "look-at-the-screen")
    assert one.description, "the example skill has no description to index"
    assert len(one.description) <= skills_mod.MAX_DESC_CHARS
