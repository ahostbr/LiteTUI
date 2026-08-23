"""The prompt lives on disk, and the skill index is a refreshable cache.

Ryan, 2026-08-22: "there should never be anything hardcoded ... move to
prompts/, but make the skills index write to skills/ as a cache, add a
'/skills refresh' cmd".

Both changes exist to remove a restart from the loop. Discovery ran once at
startup, so a skill folder written while the app was running could never be
seen — which is exactly what happened to find-claude-skills, and what the
misleading "no SKILL.md inside" message was reporting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import paths
from litetui import skills as skills_mod
from litetui.plugins.skills_plugin import _cmd_skills, _cmd_refresh


def _make_skill(base: Path, folder: str, name: str, desc: str = "does things") -> Path:
    d = base / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nBody of {name}.\n",
        encoding="utf-8",
    )
    return p


class _Settings:
    skills_enabled = True
    skill_roots: list[str] = []


class _StubApp:
    """Carries the two real methods under test, bound to a temp root."""

    def __init__(self, root: Path):
        self.root = root
        self.settings = _Settings()
        self.skills: list = []
        self.skills_cached_at = 0.0
        self.said: list[str] = []
        self.pushed: list = []

    def _system(self, text):
        self.said.append(text)

    def push_screen(self, screen, callback=None):
        self.pushed.append((screen, callback))

    def refresh_skills(self):
        from litetui import app as app_mod
        return app_mod.LiteTUI.refresh_skills(self)


# ── the prompt is a file, not a constant ─────────────────────────────────────
def test_no_tools_prompt_constant_survives_in_the_source() -> None:
    src = (Path(__file__).resolve().parent.parent / "src" / "litetui" / "app.py").read_text(
        encoding="utf-8", errors="ignore"
    )
    assert 'TOOLS_PROMPT = """' not in src, "the tools prompt is still hardcoded in app.py"
    assert "paths.TOOLS_PROMPT_FILE" in src, "nothing reads the prompt file"


def test_the_tools_prompt_file_exists_and_is_not_empty() -> None:
    assert paths.TOOLS_PROMPT_FILE.is_file(), f"{paths.TOOLS_PROMPT_FILE} is missing"
    assert paths.TOOLS_PROMPT_FILE.read_text(encoding="utf-8").strip()


# ── the cache ────────────────────────────────────────────────────────────────
def test_write_then_read_round_trips(tmp_path: Path) -> None:
    base = tmp_path / skills_mod.SKILLS_DIR_NAME
    _make_skill(base, "alpha", "alpha")
    found = skills_mod.discover_all(tmp_path, [])
    assert found, "nothing discovered to cache"

    written = skills_mod.write_cache(tmp_path, found)
    assert written == skills_mod.cache_path(tmp_path)
    assert written.is_file(), "the cache was not written where /skills says it is"

    got = skills_mod.read_cache(tmp_path)
    assert got is not None
    cached, generated = got
    assert [s.name for s in cached] == [s.name for s in found]
    assert generated > 0, "the cache carries no timestamp, so its age cannot be shown"


def test_the_cache_lands_in_the_skills_directory(tmp_path: Path) -> None:
    """Ryan named the location: the index caches beside the skills it describes."""
    p = skills_mod.cache_path(tmp_path)
    assert p.parent.name == skills_mod.SKILLS_DIR_NAME
    assert p.name == skills_mod.INDEX_CACHE_NAME


def test_a_deleted_skill_is_dropped_from_the_cache_on_read(tmp_path: Path) -> None:
    """A stale entry would fail later as a FILE error, reading as a broken skill."""
    base = tmp_path / skills_mod.SKILLS_DIR_NAME
    _make_skill(base, "alpha", "alpha")
    gone = _make_skill(base, "beta", "beta")
    skills_mod.write_cache(tmp_path, skills_mod.discover_all(tmp_path, []))

    gone.unlink()
    cached, _ = skills_mod.read_cache(tmp_path)
    assert [s.name for s in cached] == ["alpha"], "a deleted skill survived in the cache"


def test_a_corrupt_cache_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    p = skills_mod.cache_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json", encoding="utf-8")
    assert skills_mod.read_cache(tmp_path) is None

    p.write_text(json.dumps({"skills": "wrong shape"}), encoding="utf-8")
    assert skills_mod.read_cache(tmp_path) is None


# ── /skills refresh ──────────────────────────────────────────────────────────
def test_refresh_picks_up_a_skill_added_after_boot(tmp_path: Path, monkeypatch) -> None:
    """THE case that prompted all of this: a folder written while running."""
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    base = tmp_path / skills_mod.SKILLS_DIR_NAME
    _make_skill(base, "alpha", "alpha")

    a = _StubApp(tmp_path)
    a.skills = skills_mod.discover_all(tmp_path, [])
    assert [s.name for s in a.skills] == ["alpha"]

    _make_skill(base, "find-claude-skills", "find-claude-skills")
    _cmd_refresh(a)

    assert "find-claude-skills" in [s.name for s in a.skills], (
        "a skill written after boot still needs a restart"
    )
    said = "\n".join(a.said)
    assert "find-claude-skills" in said, "the delta was not reported"
    assert "+" in said, "an addition was not shown as an addition"
    assert skills_mod.cache_path(tmp_path).is_file(), "refresh did not rewrite the cache"


def test_refresh_reports_a_removal_too(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    base = tmp_path / skills_mod.SKILLS_DIR_NAME
    _make_skill(base, "alpha", "alpha")
    doomed = _make_skill(base, "beta", "beta")

    a = _StubApp(tmp_path)
    a.skills = skills_mod.discover_all(tmp_path, [])
    doomed.unlink()
    _cmd_refresh(a)

    assert [s.name for s in a.skills] == ["alpha"]
    assert "-" in "\n".join(a.said) and "beta" in "\n".join(a.said)


def test_refresh_with_no_change_says_so(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    _make_skill(tmp_path / skills_mod.SKILLS_DIR_NAME, "alpha", "alpha")
    a = _StubApp(tmp_path)
    a.skills = skills_mod.discover_all(tmp_path, [])
    _cmd_refresh(a)
    assert "no change" in "\n".join(a.said)


def test_refresh_is_not_mistaken_for_a_skill_name(tmp_path: Path, monkeypatch) -> None:
    """`/skills refresh` must refresh, not look for a skill called 'refresh'."""
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    _make_skill(tmp_path / skills_mod.SKILLS_DIR_NAME, "alpha", "alpha")
    a = _StubApp(tmp_path)
    a.skills = skills_mod.discover_all(tmp_path, [])

    _cmd_skills(a, "/skills", "refresh")
    said = "\n".join(a.said)
    assert "no skill named" not in said, "refresh was treated as a skill lookup"
    assert "after refresh" in said


def test_refresh_says_so_when_skills_are_off(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    a = _StubApp(tmp_path)
    a.settings.skills_enabled = False
    _cmd_refresh(a)
    assert "OFF" in "\n".join(a.said)
    assert not skills_mod.cache_path(tmp_path).exists(), "wrote a cache while disabled"
