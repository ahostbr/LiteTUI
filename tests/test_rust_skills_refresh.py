"""Full runtime/cache refresh uses only this test's libraries and leased app."""

import json
import os

import pytest
from test_rust_plugin_reload import app as owner_app
from test_rust_plugin_reload import armed as owner_armed
from test_rust_plugin_reload import call
from test_rust_plugin_reload import switch_app as owner_switch

from litetui import native_plugin_reload, paths, plugins, skills
from litetui import native_skills_index as index
from litetui.plugin_reload_provenance import capture_skills_baseline
from litetui.plugins import skills_plugin

app, armed, switch_app = owner_app, owner_armed, owner_switch
pytestmark = pytest.mark.asyncio


def write(directory, name, description):
    path = directory / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nPrivate body",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def library(armed, tmp_path, monkeypatch):
    root = paths.data_root()
    assert root.is_relative_to(tmp_path)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    armed.plugins = plugins.PluginRegistry()
    skills_plugin.PLUGIN.register(plugins.PluginContext(armed, armed.plugins, "skills"))
    armed.settings.skills_enabled = True
    extra = tmp_path / "versions"
    armed.settings.skill_roots = [str(extra / "*" / "skills")]
    local = root / "skills"
    write(local, "same", "local old")
    write(extra / "2.0.0/skills", "same", "old version")
    write(extra / "10.0.0/skills", "same", "selected library")
    armed.skills = skills.discover_all(root, armed.settings.skill_roots)
    cache = skills.write_cache(root, armed.skills, armed.settings.skill_roots)
    assert cache
    armed._skills_source_baseline = None
    capture_skills_baseline(armed)
    write(local, "same", "local changed")
    write(local, "new-entry", "added")
    return armed, local, cache


def review(app):
    assert "skills" in call(app, "list")["names"]
    return call(app, "review", name="skills")


def apply(app, item):
    return call(app, "apply", name="skills", token=item["token"])


def readback(app, item):
    return call(
        app, "readback", name="skills", token=item["token"], apply_id="apply-id"
    )


async def test_exact_cache_and_real_index_match_canonical_discovery(library):
    app, _, cache = library
    old = app.skills
    raw = cache.read_bytes()
    item = review(app)
    assert app.skills is old and cache.read_bytes() == raw
    assert "local changed" in item["after"] and "local old" in item["before"]
    assert apply(app, item)["cache_sha256"] == item["cache_sha256"]
    verified = readback(app, item)
    assert verified["cache_index"] == verified["live_index"] == item["after_spec"]
    canonical = skills.discover_all(paths.data_root(), app.settings.skill_roots)
    assert list(map(skills.cache_row, app.skills)) == list(
        map(skills.cache_row, canonical)
    )
    assert json.loads(cache.read_bytes())["skills"] == list(
        map(skills.cache_row, canonical)
    )
    assert "selected library" in [skill.description for skill in app.skills]
    cache.write_bytes(raw)
    with pytest.raises(ValueError, match="differs"):
        readback(app, item)


async def test_publication_rechecks_disk_guard_cache_and_no_replay(
    library, monkeypatch
):
    app, local, cache = library
    old = app.skills
    first = review(app)
    write(local, "new-entry", "raced")
    with pytest.raises(ValueError, match="changed since review"):
        apply(app, first)
    item = review(app)
    with monkeypatch.context() as m:
        m.setattr(native_plugin_reload.source, "children_pending", lambda *_: True)
        with pytest.raises(ValueError, match="deferred"):
            apply(app, item)
    with pytest.raises(ValueError, match="again"):
        apply(app, item)
    item = review(app)
    original = skills.write_cache_document

    def racing(root, data, *, before_replace, after_replace):
        cache.write_bytes(b'{"competing":"writer"}')
        return original(
            root, data, before_replace=before_replace, after_replace=after_replace
        )

    with monkeypatch.context() as m:
        m.setattr(skills, "write_cache_document", racing)
        with pytest.raises(ValueError, match="changed since review"):
            apply(app, item)
    assert app.skills is old and cache.read_bytes() == b'{"competing":"writer"}'
    item = review(app)

    def ambiguous(root, data, *, before_replace, after_replace):
        original(root, data, before_replace=before_replace)

    with monkeypatch.context() as m:
        m.setattr(skills, "write_cache_document", ambiguous)
        with pytest.raises(ValueError, match="could not be verified"):
            apply(app, item)
    assert app.skills is old
    with pytest.raises(ValueError):
        readback(app, item)


async def test_disabled_foreign_busy_and_unsupported_layout_skip_publication(
    library, monkeypatch
):
    app, local, cache = library
    raw = cache.read_bytes()
    app.settings.skills_enabled = False
    with pytest.raises(ValueError, match="disabled"):
        review(app)
    app.settings.skills_enabled = True
    with pytest.raises(ValueError):
        call(app, "review", name="skills", work_instance="foreign")
    old = app.settings.skill_roots
    for unsupported in ("../user-library", str(local / "**" / "skills")):
        app.settings.skill_roots = [unsupported]
        with pytest.raises(ValueError, match="bounded|absolute"):
            review(app)
    app.settings.skill_roots = old
    with monkeypatch.context() as m:
        m.setattr(index, "MAX_ENTRIES", 1)
        with pytest.raises(ValueError, match="bounded"):
            review(app)
    path = write(local, "linked", "metadata")
    os.link(path, local / "extra-link")
    with pytest.raises(ValueError, match="unlinked"):
        review(app)
    assert cache.read_bytes() == raw


async def test_empty_index_keeps_explicit_cache_evidence(library):
    app, local, cache = library
    app.settings.skill_roots = []
    for path in local.glob("*/SKILL.md"):
        path.unlink()
    item = review(app)
    apply(app, item)
    assert readback(app, item)["cache_index"] == item["after_spec"]
    assert app.skills == [] and json.loads(cache.read_bytes())["skills"] == []


async def test_held_cache_lock_and_missing_boot_provenance_leave_index_untouched(
    library,
):
    from litetui.shared_state import coordinated_write

    app, _, cache = library
    before = cache.read_bytes()
    live = app.skills
    item = review(app)
    with (
        coordinated_write(cache, timeout=0),
        pytest.raises(ValueError, match="could not be verified"),
    ):
        apply(app, item)
    item = review(app)
    app._skills_source_baseline["files"].clear()
    with pytest.raises(ValueError, match="restart"):
        apply(app, item)
    assert cache.read_bytes() == before and app.skills is live
