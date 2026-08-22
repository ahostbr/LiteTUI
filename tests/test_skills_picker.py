"""Skills: a slash-tolerant name, an honest skip reason, and a picker.

Three faults, all found from one screenshot (Ryan, 2026-08-22):

  1. "/ls-mark" failed against a list that visibly CONTAINED ls-mark. The
     lookup lowercased and stripped whitespace but not the leading slash --
     which is how every skill is written and spoken, so it is what both a user
     and a model type.

  2. A folder created after startup was reported as "no SKILL.md inside" while
     holding a 3,292-byte SKILL.md. The reason was INFERRED from `name not in
     loaded` and never checked, so the message named a cause that was not just
     unverified but false -- and sent the reader off to write a file that
     already existed. The real answer (discovery runs once at startup, restart
     to pick it up) was the one thing it could never say.

  3. /skills answered "what do I have" with ~80 lines of transcript. It is a
     list to browse, so it gets the same picker /convos and /model already use.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import skills as skills_mod
from picker import PickerScreen
from plugins.skills_plugin import _cmd_skills, _skipped_lines, _REPORT_ROW


def _skill(tmp_path: Path, name: str, body: str = "the body") -> skills_mod.Skill:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return skills_mod.Skill(name, f"{name} does things", p, "local")


class _Settings:
    skills_enabled = True
    skill_roots: list[str] = []


class _StubApp:
    def __init__(self, skills):
        self.skills = skills
        self.settings = _Settings()
        self.said: list[str] = []
        self.pushed: list = []
        # Carried so "shown" and "sent" are distinguishable. Without it this
        # stub could not fail on a skill that was displayed and never
        # delivered, which is exactly what happened.
        self.conversation: list[dict] = [{"role": "system", "content": "BASE"}]

    def _system(self, text):
        self.said.append(text)

    def _append_to_system(self, text: str) -> None:
        current = self.conversation[0].get("content") or ""
        if text in current:
            return
        self.conversation[0] = {
            **self.conversation[0],
            "content": (current.rstrip() + "\n\n" + text) if current else text,
        }

    def push_screen(self, screen, callback=None):
        self.pushed.append((screen, callback))


# ── 1. the slash ─────────────────────────────────────────────────────────────
def test_a_leading_slash_still_finds_the_skill(tmp_path: Path) -> None:
    skills = [_skill(tmp_path, "ls-mark", "MARK BODY")]
    assert skills_mod.load(skills, "/ls-mark") == "MARK BODY"
    assert skills_mod.load(skills, "ls-mark") == "MARK BODY", "the bare name still works"
    assert skills_mod.load(skills, "/LS-MARK") == "MARK BODY", "still case-insensitive"


def test_a_genuinely_absent_skill_still_errors(tmp_path: Path) -> None:
    """The slash fix must not turn every miss into a match."""
    skills = [_skill(tmp_path, "ls-mark")]
    out = skills_mod.load(skills, "/nope")
    assert out.startswith("[error]") and "ls-mark" in out, "the miss stopped listing what exists"


# ── 2. the skip reason ───────────────────────────────────────────────────────
def test_an_unloaded_folder_WITH_a_skill_md_says_restart(tmp_path: Path) -> None:
    (tmp_path / "find-claude-skills").mkdir()
    (tmp_path / "find-claude-skills" / "SKILL.md").write_text("x", encoding="utf-8")
    app = _StubApp([])
    lines = "\n".join(_skipped_lines(app, tmp_path))
    assert "find-claude-skills" in lines
    assert "restart" in lines.lower(), f"the real reason is not stated: {lines!r}"
    assert "no SKILL.md inside" not in lines, (
        "it still claims the file is missing while the file is right there"
    )


def test_a_folder_without_a_skill_md_still_says_so(tmp_path: Path) -> None:
    (tmp_path / "scratch").mkdir()
    app = _StubApp([])
    lines = "\n".join(_skipped_lines(app, tmp_path))
    assert "no SKILL.md inside" in lines and "scratch" in lines


def test_both_reasons_can_appear_together(tmp_path: Path) -> None:
    (tmp_path / "scratch").mkdir()
    (tmp_path / "fresh").mkdir()
    (tmp_path / "fresh" / "SKILL.md").write_text("x", encoding="utf-8")
    lines = "\n".join(_skipped_lines(_StubApp([]), tmp_path))
    assert "restart" in lines.lower() and "no SKILL.md inside" in lines


def test_a_loaded_folder_is_not_reported_at_all(tmp_path: Path) -> None:
    s = _skill(tmp_path, "ls-mark")
    assert _skipped_lines(_StubApp([s]), tmp_path) == []


# ── 3. the picker ────────────────────────────────────────────────────────────
def test_skills_opens_a_picker_instead_of_dumping(tmp_path: Path, monkeypatch) -> None:
    import paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    skills = [_skill(tmp_path, "ls-mark"), _skill(tmp_path, "ls-arch")]
    app = _StubApp(skills)

    _cmd_skills(app, "/skills", "")

    assert app.pushed, "no picker was opened — /skills is still printing to chat"
    screen, _cb = app.pushed[0]
    assert isinstance(screen, PickerScreen)
    ids = [row_id for row_id, _label in screen._rows]
    assert ids[0] == _REPORT_ROW, "the full report is no longer reachable from the picker"
    assert "ls-arch" in ids and "ls-mark" in ids
    assert ids[1:] == sorted(ids[1:]), "skills are not in a predictable order"


def test_picking_a_skill_sends_its_body_to_the_model(tmp_path: Path, monkeypatch) -> None:
    """AMENDED 2026-08-22. Was `test_picking_a_skill_shows_its_body` and
    asserted exactly that — the body in app.said, the SCREEN. It passed
    throughout the period the picker sent nothing to the model, because
    _StubApp had no conversation for it to fail against. A stub that cannot
    represent the missing behaviour cannot catch its absence."""
    import paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    app = _StubApp([_skill(tmp_path, "ls-mark", "MARK BODY")])
    _cmd_skills(app, "/skills", "")
    _screen, callback = app.pushed[0]

    callback("ls-mark")
    assert "MARK BODY" in app.conversation[0]["content"], "the picker sent nothing"
    assert not any("MARK BODY" in s for s in app.said), "the body still hits the log"

    callback(None)  # Esc must be inert, not an error
    assert not any("[error]" in s for s in app.said)


def test_an_empty_library_still_explains_where_it_looked(tmp_path: Path, monkeypatch) -> None:
    """With nothing to pick, the report IS the useful answer."""
    import paths
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    app = _StubApp([])
    _cmd_skills(app, "/skills", "")
    assert not app.pushed, "opened an empty picker"
    assert app.said, "said nothing at all"
