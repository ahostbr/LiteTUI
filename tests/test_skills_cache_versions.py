"""Plugin upgrades invalidate the real startup index, without touching user caches."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import appsvc, paths, skills


def _skill(root: Path, body: str) -> Path:
    path = root / "ls-youtube" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: ls-youtube\ndescription: {body}\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def startup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    app = SimpleNamespace(settings=SimpleNamespace(skills_enabled=True, skill_roots=[]))
    return app


@pytest.mark.parametrize("legacy", [False, True])
def test_upgrade_replaces_cached_old_plugin_and_preserves_other_libraries(tmp_path, startup, legacy):
    plugin = tmp_path / "plugins" / "cache" / "liteharness" / "liteharness"
    local = _skill(tmp_path / "skills", "LOCAL")
    claude = _skill(tmp_path / ".claude" / "skills", "CLAUDE")
    old = _skill(plugin / "1.0.16" / "skills", "PLUGIN 16")
    startup.settings.skill_roots = [str(claude.parent.parent), str(plugin / "*" / "skills")]
    before, _ = appsvc.load_skills(startup)
    assert {s.name: s.path for s in before} == {
        "ls-youtube": local,
        "ls-youtube@claude": claude,
        "ls-youtube@liteharness": old,
    }
    if legacy:
        cache = skills.cache_path(tmp_path)
        raw = json.loads(cache.read_text(encoding="utf-8"))
        del raw["roots"]
        cache.write_text(json.dumps(raw), encoding="utf-8")

    new = _skill(plugin / "1.0.18" / "skills", "PLUGIN 18")
    # An old extracted/touched install can have a NEWER mtime than the upgrade.
    os.utime(new.parent.parent, (1_000_000_000, 1_000_000_000))
    os.utime(old.parent.parent, (1_100_000_000, 1_100_000_000))
    after, generated = appsvc.load_skills(startup)
    assert generated == 0.0
    assert {s.name: s.path for s in after} == {
        "ls-youtube": local,
        "ls-youtube@claude": claude,
        "ls-youtube@liteharness": new,
    }
    assert "PLUGIN 18" in skills.load(after, "ls-youtube@liteharness")
    assert str(new.parent) in skills.load(after, "ls-youtube@liteharness")
    assert old.is_file(), "invalidation must not require deleting old installations"
    persisted, timestamp = skills.read_cache(tmp_path, startup.settings.skill_roots)
    assert timestamp > 0
    assert [s.path for s in persisted] == [s.path for s in after]


def test_unchanged_roots_reuse_index_without_rescanning_bodies(tmp_path, startup, monkeypatch):
    path = _skill(tmp_path / "skills", "LOCAL")
    appsvc.load_skills(startup)

    def unexpected_discovery(*args):
        pytest.fail("unchanged selected roots should reuse the cache")

    monkeypatch.setattr(skills, "discover_all", unexpected_discovery)
    cached, generated = appsvc.load_skills(startup)
    assert generated > 0
    assert cached[0].path == path


def test_configured_roots_add_reorder_and_remove_without_losing_collisions(tmp_path, startup):
    a = _skill(tmp_path / "libA" / "skills", "A")
    b = _skill(tmp_path / "libB" / "skills", "B")
    startup.settings.skill_roots = [str(a.parent.parent)]
    appsvc.load_skills(startup)
    for roots, expected in [
        ([a, b], {"ls-youtube": a, "ls-youtube@libB": b}),
        ([b, a], {"ls-youtube": b, "ls-youtube@libA": a}),
        ([b], {"ls-youtube": b}),
        ([], {}),
    ]:
        startup.settings.skill_roots = [str(p.parent.parent) for p in roots]
        found, generated = appsvc.load_skills(startup)
        assert generated == 0.0
        assert {s.name: s.path for s in found} == expected


def test_previously_missing_root_becoming_available_invalidates_index(tmp_path, startup):
    _skill(tmp_path / "skills", "LOCAL")
    missing = tmp_path / "library" / "skills"
    startup.settings.skill_roots = [str(missing)]
    appsvc.load_skills(startup)
    added = _skill(missing, "ADDED")
    found, generated = appsvc.load_skills(startup)
    assert generated == 0.0
    assert {s.name: s.path for s in found}["ls-youtube@library"] == added


def test_legacy_cache_is_rewritten_even_without_an_upgrade(tmp_path, startup):
    _skill(tmp_path / "skills", "LOCAL")
    appsvc.load_skills(startup)
    cache = skills.cache_path(tmp_path)
    raw = json.loads(cache.read_text(encoding="utf-8"))
    del raw["roots"]
    cache.write_text(json.dumps(raw), encoding="utf-8")
    _, generated = appsvc.load_skills(startup)
    assert generated == 0.0
    assert "roots" in json.loads(cache.read_text(encoding="utf-8"))
    assert appsvc.load_skills(startup)[1] > 0


def test_version_order_is_numeric_and_explicit_pins_remain_pinned(tmp_path):
    for version in ("1.0.9", "1.0.18", "1.0.100"):
        _skill(tmp_path / version / "skills", version)
    old = tmp_path / "1.0.9" / "skills"
    os.utime(old, (2_000_000_000, 2_000_000_000))
    assert skills.resolve_roots([str(tmp_path / "*" / "skills")]) == [
        (tmp_path / "1.0.100" / "skills").resolve()
    ]
    assert skills.resolve_roots([str(old)]) == [old.resolve()]


def test_nonversioned_globs_keep_mtime_selection(tmp_path):
    old = tmp_path / "z-old" / "skills"
    new = tmp_path / "a-new" / "skills"
    _skill(old, "OLD")
    _skill(new, "NEW")
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (1_100_000_000, 1_100_000_000))
    assert skills.resolve_roots([str(tmp_path / "*" / "skills")]) == [new.resolve()]
