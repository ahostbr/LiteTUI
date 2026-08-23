"""The slash picker must list the app's OWN commands, not only skills.

Ryan: "note the new autocomplete feature u added last session... it doesnt list
are standard app slash commands only skills both need to be present in the list"

Half the feature shipped. The widget is called SkillAutocomplete and it did
exactly what its name says, so a user typing `/` was offered a skills library
while /settings /convos /model /rename /themes and ~20 other commands — the
app's own surface, the things that always exist — were invisible to the one
affordance built for finding them.

THE WORSE HALF was the guard: `if not self.skills or ...` meant a user with an
EMPTY skills library got no picker at all, for any command. Test 1 is that case
and a populated-library fixture cannot catch it.

THE ORDERING IS THE BEHAVIOUR, because Tab takes the first row:

  1. commands sort before skills at equal prefix quality — they are the app's
     own surface and they always exist; a skills library may be empty.
  2. commands are ordered by PALETTE order (plugins.palette_sort_key), not
     alphabetically, so this list and the command palette cannot drift.
  3. DEDUPE WITH COMMANDS WINNING, matching dispatch exactly: _handle_command
     resolves a bare name to a skill ONLY when no command matches. A row
     offering a skill that a command shadows would be lying about what Enter
     does.
  4. one row per COMMAND, not per alias. The registry is keyed by every alias
     (41 keys, 27 entries), so the naive read shows /model twice and /new
     three times.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import skills as skills_mod


async def _settle(pilot, cond, ceiling: float = 3.0) -> bool:
    """Pump the event loop until cond() holds, or the ceiling passes.

    ONE `await pilot.pause()` yields ONE frame. Assigning `box.value` posts
    Input.Changed -> handler -> sync_skill_autocomplete -> display flip, and on
    a loaded box that chain can need more than one frame. Caught 2026-08-22:
    the full suite failed this file's last test once and passed it on an
    IDENTICAL tree minutes later (980+1, then 981) — the signature of a race,
    not of a wrong assertion.

    Note the test above is structurally immune because it calls
    sync_skill_autocomplete DIRECTLY; only the message-driven path can lose.

    Polling rather than a longer pause, for the same reason _wait in
    test_tool_cancel.py polls: a bigger fixed sleep is the same defect, slower,
    and it is paid on EVERY run instead of only on a slow one. This ceiling is
    reached only when the thing genuinely never happens.
    """
    deadline = time.monotonic() + ceiling
    while True:
        if cond():
            return True
        if time.monotonic() >= deadline:
            return False
        await pilot.pause()


def _skills(tmp_path: Path, *names) -> list:
    out = []
    for n in names:
        d = tmp_path / n
        d.mkdir(parents=True, exist_ok=True)
        p = d / "SKILL.md"
        p.write_text(f"body of {n}", encoding="utf-8")
        out.append(skills_mod.Skill(n, f"{n} does things", p, "local"))
    return out


def make_app(tmp_path: Path, *skill_names):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = []
    a.skills = _skills(tmp_path, *skill_names) if skill_names else []
    return a


def _rows(a) -> list:
    """(name, kind) for what the picker is currently offering."""
    return [(c.name, c.kind) for c in a._skill_ac._matches]


def _names(a) -> list[str]:
    return [n for n, _ in _rows(a)]


# ── 1. NEGATIVE CONTROL — zero skills, commands still listed ─────────────

@pytest.mark.asyncio
async def test_commands_are_listed_when_there_are_no_skills_at_all(tmp_path: Path):
    """The guard was `if not self.skills`, so an empty library disabled the
    picker for ~20 commands that do exist. A fixture with skills in it cannot
    fail this test, which is exactly why it is the first one."""
    a = make_app(tmp_path)                      # no skills
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert a.skills == [], "precondition: the library must be empty"

        a.sync_skill_autocomplete("/set")
        assert a._skill_ac.display, (
            "an empty skills library left the user with no completion at all"
        )
        assert "settings" in _names(a), f"/settings was not offered: {_names(a)}"


# ── 2. both kinds appear, and the ORDER is asserted ──────────────────────

@pytest.mark.asyncio
async def test_a_prefix_matching_both_offers_both_with_the_command_first(tmp_path):
    a = make_app(tmp_path, "compact-notes")
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a.sync_skill_autocomplete("/compact")
        rows = _rows(a)

    names = [n for n, _ in rows]
    assert "compact" in names, f"the command is missing: {rows}"
    assert "compact-notes" in names, f"the skill is missing: {rows}"
    assert names.index("compact") < names.index("compact-notes"), (
        f"the skill outranked the command, and Tab takes the first row: {rows}"
    )
    assert rows[0] == ("compact", "command")


# ── 3. a command SHADOWS a same-named skill, matching dispatch ───────────

@pytest.mark.asyncio
async def test_a_skill_named_after_a_command_is_not_offered(tmp_path: Path):
    """_handle_command tries plugins.commands FIRST and only falls through to
    a skill when nothing matched. So `/model` runs the command, always. A
    picker that offered the skill would be advertising something Enter will
    never do."""
    a = make_app(tmp_path, "model")
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a.sync_skill_autocomplete("/model")
        rows = _rows(a)

    assert ("model", "command") in rows, f"the command is missing: {rows}"
    assert ("model", "skill") not in rows, (
        f"a shadowed skill was offered; dispatch would run the command: {rows}"
    )
    assert [n for n, _ in rows].count("model") == 1, f"duplicated row: {rows}"


# ── 4. CONTROL — skill-only behaviour is untouched ───────────────────────

@pytest.mark.asyncio
async def test_skills_are_still_offered_and_still_complete(tmp_path: Path):
    """This must not become a commands-only list. `ls-` matches no command,
    so this is the pure-skill path end to end, including Tab."""
    a = make_app(tmp_path, "ls-arch", "ls-mark")
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        box = a.query_one("#message-input", m.Input)
        box.focus()
        box.value = "/ls-m"

        assert await _settle(pilot, lambda: a._skill_ac.display), (
            "the picker did not open on a skill name"
        )
        assert _rows(a) == [("ls-mark", "skill")], f"unexpected rows: {_rows(a)}"

        await pilot.press("tab")
        assert await _settle(pilot, lambda: box.value == "/ls-mark "), (
            f"tab did not complete: {box.value!r}"
        )


# ── 5. one row per command, not one per ALIAS ────────────────────────────

@pytest.mark.asyncio
async def test_aliases_collapse_to_the_primary_token(tmp_path: Path):
    """`/model` and `/models` are ONE CommandEntry reached by two keys; `/new`
    `/clear` `/reset` are one reached by three. The registry is keyed by every
    alias, so the naive read shows the same command several times."""
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a.sync_skill_autocomplete("/model")
        model_rows = _names(a)
        a.sync_skill_autocomplete("/")
        every = _names(a)

    assert "models" not in model_rows, (
        f"the alias /models was offered as its own row: {model_rows}"
    )
    assert model_rows.count("model") == 1, f"duplicated: {model_rows}"

    # Across the WHOLE list, no alias may appear beside its primary.
    for alias, primary in (("clear", "new"), ("reset", "new"),
                           ("config", "settings"), ("exit", "quit"),
                           ("cal", "calendar"), ("skill", "skills")):
        assert primary in every, f"{primary} missing from the full list"
        assert alias not in every, (
            f"alias {alias!r} was listed beside its primary {primary!r}"
        )
    assert len(every) == len(set(every)), f"duplicate rows: {every}"


# ── 6. commands follow PALETTE order, not the alphabet ───────────────────

@pytest.mark.asyncio
async def test_commands_use_the_palette_order_rather_than_a_second_ordering(tmp_path):
    """PALETTE_GROUPS is Ryan's ordering (convo, backend, tools, automation,
    screen, app) and it already exists. A second copy of an order is how two
    lists drift apart, so this asserts the shared one is the one in use."""
    a = make_app(tmp_path)
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a.sync_skill_autocomplete("/")
        names = _names(a)

    # convo group before backend before app — alphabetically this order is
    # impossible (new > model > help), so it can only come from the palette.
    assert names.index("new") < names.index("model"), (
        f"convo did not precede backend: {names[:12]}"
    )
    assert names.index("model") < names.index("help"), (
        f"backend did not precede app: {names[:12]}"
    )
    assert names.index("quit") < len(names), "quit missing"


# ── 7. a row says which KIND it is, on screen ────────────────────────────

@pytest.mark.asyncio
async def test_each_row_is_labelled_command_or_skill_on_screen(tmp_path: Path):
    """The user must be able to tell a command from a skill WITHOUT running
    it. Asserted on the rendered option text, not on the model behind it."""
    a = make_app(tmp_path, "compact-notes")
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        a.sync_skill_autocomplete("/compact")
        rendered = [
            str(a._skill_ac.options.get_option_at_index(i).prompt)
            for i in range(a._skill_ac.options.option_count)
        ]

    joined = "\n".join(rendered)
    assert "command" in joined.lower(), f"no command label on screen: {rendered}"
    assert "skill" in joined.lower(), f"no skill label on screen: {rendered}"
    cmd_row = next(r for r in rendered if r.strip().startswith("compact "))
    assert "command" in cmd_row.lower(), f"the command row is unlabelled: {cmd_row!r}"


# ── 8. completing a COMMAND lands on something real ──────────────────────

@pytest.mark.asyncio
async def test_completing_a_command_produces_a_command_that_runs(tmp_path: Path):
    """The defect class this repo keeps finding is a control that does
    nothing. Tab must yield a string _handle_command actually dispatches."""
    a = make_app(tmp_path)
    said: list[str] = []
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        box = a.query_one("#message-input", m.Input)
        box.focus()
        box.value = "/rena"
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert box.value == "/rename ", f"tab did not complete: {box.value!r}"

        a._system = lambda s, *x, **k: said.append(str(s))
        a._handle_command("/rename")
        await pilot.pause()

    assert "Unknown" not in "\n".join(said), (
        f"the completed command was dead: {said!r}"
    )
