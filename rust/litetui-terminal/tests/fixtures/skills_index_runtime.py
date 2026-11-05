"""Register the real Skills plugin against only private libraries and cache."""

import importlib.util
import json
import os
from pathlib import Path

from litetui import plugins, skills
from litetui.plugin_reload_provenance import capture_skills_baseline
from litetui.plugins import skills_plugin

root = Path(os.environ["LITETUI_DATA_ROOT"])
spec = importlib.util.spec_from_file_location(
    "_skills_boundaries", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
)
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed, original_serve = common.seed, common.serve


def seed(app):
    original_seed(app)
    app.settings.skills_enabled = True
    app.settings.skill_roots = []
    app.plugins = plugins.PluginRegistry()
    skills_plugin.PLUGIN.register(plugins.PluginContext(app, app.plugins, "skills"))
    app._skills_source_baseline = None
    capture_skills_baseline(app)
    app.skills = []
    assert skills.write_cache(root, [], [])
    path = root / "skills/private-entry/SKILL.md"
    path.parent.mkdir()
    path.write_text(
        "---\nname: private-entry\ndescription: Private refreshed metadata\n---\nNever execute this body",
        encoding="utf-8",
    )


async def serve(app):
    # The canonical activity gate inspects the current user's durable children.
    # Isolate its home as well as the canonical root and configured libraries.
    old = {key: os.environ.get(key) for key in ("USERPROFILE", "HOME")}
    os.environ.update(USERPROFILE=str(root), HOME=str(root))
    try:
        await original_serve(app)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        (root / "skills-after.json").write_text(
            json.dumps([skills.cache_row(s) for s in app.skills]), encoding="utf-8"
        )


common.seed = seed
common.history.serve = serve
if __name__ == "__main__":
    common.history.main()
