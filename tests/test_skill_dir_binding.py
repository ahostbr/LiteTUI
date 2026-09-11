"""T582 — a skill body must name the directory it was actually loaded from.

A LiteTUI/Codex seat (0be01da8, 2026-09-10 22:0x) asked for `/ls-mark`, got back
SKILL.md VERBATIM, and read a run line pointing at `<this skill's directory>`.
Nothing resolves that. The only skills directory the system prompt named was
`<root>/skills` — where the REPO's own skills live, not where this one came
from — so the seat guessed `C:/Projects/LiteTUI/skills/ls-mark/mark.py`, missed,
and hand-searched the plugin cache across two installed versions before it could
run a one-line tool.

🔴 THE CARD BLAMED THE SKILL TEXT; THE MEASUREMENT SAYS OTHERWISE. No SKILL.md
on this machine names a repo path — that literal appears in exactly one file,
the seat's own transcript, because the seat INFERRED it. What the bodies carry
is a placeholder, in two spellings, and 25 of the discovered skills carry one
(100 occurrences, measured 2026-09-10). A defect that reaches 25 files through
one function is a defect in the function.

⚠️ THESE ARMS ASSERT THE BINDING, NOT THE SPELLING. A skill that invents a third
placeholder tomorrow is still broken and `test_no_placeholder_survives_load`
would still pass — it can only see the two spellings that exist. That arm is a
regression guard on today's library, not a proof about all future skill text.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import paths  # noqa: E402
from litetui import skills as skills_mod  # noqa: E402
from litetui import textfmt  # noqa: E402


def _skill(tmp_path: Path, name: str, body: str) -> list[skills_mod.Skill]:
    """One skill on disk, discovered the way the app discovers it."""
    d = tmp_path / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: a test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skills_mod.discover_dir(tmp_path / "skills", "test")


# ── the binding itself ──────────────────────────────────────────────


def test_load_substitutes_both_spellings(tmp_path: Path) -> None:
    """Both placeholders are replaced with the directory the skill came from."""
    body = (
        'python "<this skill\'s directory>/mark.py"\n'
        'python "${CLAUDE_SKILL_DIR}/lms_switch.py" status\n'
    )
    found = _skill(tmp_path, "ls-probe", body)
    out = skills_mod.load(found, "ls-probe")

    here = str(tmp_path / "skills" / "ls-probe")
    assert "<this skill's directory>" not in out
    assert "${CLAUDE_SKILL_DIR}" not in out
    assert f'python "{here}/mark.py"' in out
    assert f'python "{here}/lms_switch.py" status' in out


def test_load_prepends_the_base_directory(tmp_path: Path) -> None:
    """The header is the part that survives a placeholder nobody anticipated:
    even if the body names its files some third way, the reader is told where
    the skill lives before it reads a single command."""
    found = _skill(tmp_path, "ls-plain", "no placeholders here at all")
    out = skills_mod.load(found, "ls-plain")

    first = out.splitlines()[0]
    assert first == f"Base directory for this skill: {tmp_path / 'skills' / 'ls-plain'}"
    assert "no placeholders here at all" in out


def test_the_directory_is_where_it_LOADED_from_not_the_repo(tmp_path: Path) -> None:
    """The seat's actual mistake, as an assertion.

    It reasoned from the one skills directory it had been told about. So this
    arm fails if the binding ever resolves against `paths.ROOT` instead of the
    skill's own path — which is the shape the bug would take on the way back.
    """
    found = _skill(tmp_path, "ls-elsewhere", 'run "<this skill\'s directory>/x.py"')
    out = skills_mod.load(found, "ls-elsewhere")

    assert str(tmp_path) in out
    assert str(paths.ROOT / "skills") not in out


def test_a_leading_slash_still_resolves(tmp_path: Path) -> None:
    """`/ls-probe` is how a skill is spoken and typed. The binding must not sit
    on a path the slash-stripping branch skips."""
    found = _skill(tmp_path, "ls-probe", 'run "<this skill\'s directory>/x.py"')
    assert "<this skill's directory>" not in skills_mod.load(found, "/ls-probe")


def test_an_unknown_name_is_not_given_a_base_directory(tmp_path: Path) -> None:
    """The error path returns a message, not a body. Prefixing THAT with a
    directory header would read as a skill that loaded and came back empty."""
    found = _skill(tmp_path, "ls-probe", "body")
    out = skills_mod.load(found, "ls-nope")
    assert out.startswith("[error]")
    assert "Base directory" not in out


def test_no_placeholder_survives_load() -> None:
    """The library as it stands on this machine: 25 skills, 100 occurrences
    before the fix. Scoped to the two spellings that exist — see the docblock."""
    found = skills_mod.discover_all(paths.ROOT, None)
    if not found:
        return  # a machine with no skill library proves nothing either way
    leaked = [
        s.name
        for s in found
        for token in skills_mod.SKILL_DIR_PLACEHOLDERS
        if token in skills_mod.load(found, s.name)
    ]
    assert not leaked, f"skills still handing back an unbound placeholder: {leaked}"


# ── the per-file prompt override (same card, second half) ───────────


def test_a_partial_prompt_override_does_not_move_the_others(
    tmp_path: Path, monkeypatch
) -> None:
    """`prompts/__init__.py` invites a dev to override ANY ONE file. The anchor
    was all-or-nothing on systemprompt.md, so accepting that invitation moved
    every other read into a directory that did not have them — and
    `textfmt.load_prompt` reads unguarded, so it raised FileNotFoundError."""
    repo = tmp_path / "prompts"
    repo.mkdir()
    (repo / "systemprompt.md").write_text("overridden base prompt", encoding="utf-8")
    monkeypatch.setattr(paths, "_REPO_PROMPTS", repo)

    assert paths.prompt_file("systemprompt.md") == repo / "systemprompt.md"
    # every OTHER prompt still resolves to where the files actually ship
    for other in ("compact.md", "tool-denied.md", "tools.md"):
        assert paths.prompt_file(other) == paths.PROMPTS_DIR / other
        assert paths.prompt_file(other).is_file(), other


def test_load_prompt_reads_through_the_per_file_resolver(
    tmp_path: Path, monkeypatch
) -> None:
    """The negative control for the arm above: with the override in place, the
    reader that used to crash must still find its file and return its text."""
    repo = tmp_path / "prompts"
    repo.mkdir()
    (repo / "systemprompt.md").write_text("overridden", encoding="utf-8")
    monkeypatch.setattr(paths, "_REPO_PROMPTS", repo)

    assert textfmt.load_prompt("compact").strip()
