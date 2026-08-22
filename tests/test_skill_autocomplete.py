"""The skill picker rises from the message area and completes on Tab.

Ryan's spec: "the skills picker pops up from the message area vertically and
lists skills so that the user either keeping typing with a press tab to
autocomplete skill name feature and a clickable / scrollable menu for them to
either arrow key up and down to nav or scroll wheel or click."

Two design points asserted here because both could have gone the other way:

  1. IT IS NOT A MODAL. A modal takes focus, and focus is what the typing needs
     -- the whole interaction is "keep typing and watch the list narrow". So the
     Input keeps focus and the keys are intercepted on it.

  2. THE COMPLETION HAS TO LAND SOMEWHERE REAL. Before this, an unknown slash
     name printed "Unknown: /ls-mark" beside a list that contained ls-mark --
     there was no command-to-skill fallback at all. Completing to a dead
     command would be a control that does nothing, which is the defect class
     this repo keeps finding. `/name` now loads that skill on an EXACT match.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import skills as skills_mod


def _skills(tmp_path: Path, *names) -> list:
    out = []
    for n in names:
        d = tmp_path / n
        d.mkdir(parents=True, exist_ok=True)
        p = d / "SKILL.md"
        p.write_text(f"body of {n}", encoding="utf-8")
        out.append(skills_mod.Skill(n, f"{n} does things", p, "local"))
    return out


def make_app(tmp_path: Path):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = []
    a.skills = _skills(tmp_path, "ls-arch", "ls-arch-fable", "ls-mark", "banana-pro")
    return a


@pytest.mark.asyncio
async def test_it_opens_on_a_slash_name_and_hides_otherwise(tmp_path: Path) -> None:
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        ac = a._skill_ac
        assert not ac.display, "the picker is open before anything was typed"

        a.sync_skill_autocomplete("/ls-")
        assert ac.display, "typing a slash name did not open the picker"

        # An argument means the user has moved on; a list reopening under one
        # is noise.
        a.sync_skill_autocomplete("/ls-mark now")
        assert not ac.display, "the picker stayed open past the space"

        a.sync_skill_autocomplete("hello there")
        assert not ac.display, "the picker opened on ordinary prose"


@pytest.mark.asyncio
async def test_a_prefix_match_outranks_a_substring(tmp_path: Path) -> None:
    """Tab takes the first row, so ordering IS the behaviour."""
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a.sync_skill_autocomplete("/arch")
        # "ls-arch" merely CONTAINS arch; nothing starts with it, so both are
        # substring hits and sort alphabetically.
        assert a._skill_ac.current() == "ls-arch"

        a.sync_skill_autocomplete("/ls-arch")
        assert a._skill_ac.current() == "ls-arch", "the exact prefix lost to a longer name"


@pytest.mark.asyncio
async def test_tab_completes_the_highlighted_name(tmp_path: Path) -> None:
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        box = a.query_one("#message-input", m.Input)
        box.focus()
        await pilot.pause()

        box.value = "/ls-m"
        await pilot.pause()          # Input.Changed drives the filter
        assert a._skill_ac.display, "typing did not open the picker"

        await pilot.press("tab")
        await pilot.pause()
        assert box.value == "/ls-mark ", f"tab did not complete: {box.value!r}"
        assert not a._skill_ac.display, "the picker stayed open after completing"


@pytest.mark.asyncio
async def test_arrows_move_the_highlight_without_touching_the_text(tmp_path: Path) -> None:
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        box = a.query_one("#message-input", m.Input)
        box.focus()
        box.value = "/ls-"
        await pilot.pause()

        first = a._skill_ac.current()
        await pilot.press("down")
        await pilot.pause()
        second = a._skill_ac.current()
        assert second != first, "down did not move the highlight"
        assert box.value == "/ls-", "arrow keys edited the message text"

        await pilot.press("up")
        await pilot.pause()
        assert a._skill_ac.current() == first, "up did not come back"


@pytest.mark.asyncio
async def test_escape_dismisses_and_returns_the_keys(tmp_path: Path) -> None:
    """A widget that keeps a key it no longer needs is how tab-to-focus dies."""
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        box = a.query_one("#message-input", m.Input)
        box.focus()
        box.value = "/ls-"
        await pilot.pause()
        assert a._skill_ac.display

        await pilot.press("escape")
        await pilot.pause()
        assert not a._skill_ac.display, "escape did not dismiss the picker"

        before = box.value
        await pilot.press("tab")
        await pilot.pause()
        assert box.value == before, "tab was still being swallowed after dismissal"


@pytest.mark.asyncio
async def test_the_input_keeps_focus_throughout(tmp_path: Path) -> None:
    """The filtering IS the typing. A picker that takes focus cannot work."""
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        box = a.query_one("#message-input", m.Input)
        box.focus()
        box.value = "/ls-"
        await pilot.pause()
        assert a._skill_ac.display
        assert a.focused is box, "the picker stole focus from the message box"


@pytest.mark.asyncio
async def test_a_completed_name_actually_runs_the_skill(tmp_path: Path) -> None:
    """The completion must land on something real, not a dead command."""
    a = make_app(tmp_path)
    said: list[str] = []
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a._system = lambda s: said.append(s)
        a._handle_command("/ls-mark")
        await pilot.pause()

    joined = "\n".join(said)
    assert "Unknown" not in joined, f"a completed skill name is still a dead command: {joined!r}"
    # Amended 2026-08-22: this asserted the body was SHOWN, which is the defect
    # Ryan reported — the command painted the skill and sent nothing. The real
    # contract is that it reaches the MODEL, so that is what is asserted now.
    assert "body of ls-mark" in a.conversation[0]["content"], (
        "the completed skill name did not deliver the body to the model"
    )
    assert "body of ls-mark" not in joined, "the body is still being dumped to the log"


@pytest.mark.asyncio
async def test_a_genuine_typo_still_reports_itself(tmp_path: Path) -> None:
    """Only EXACT matches fall through, or a mistyped command loads a lookalike."""
    a = make_app(tmp_path)
    said: list[str] = []
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a._system = lambda s: said.append(s)
        a._handle_command("/ls-mar")
        await pilot.pause()
    assert "Unknown" in "\n".join(said), "a typo silently loaded a skill"
