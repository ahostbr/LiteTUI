"""Seed private Context documents and observe the unchanged real source/runtime entry point."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path


def load_common():
    fixture = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"]).resolve(strict=True)
    spec = importlib.util.spec_from_file_location("_context_boundaries", fixture)
    assert spec and spec.loader
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    return runtime


common = load_common()
original_seed = common.seed
original_serve = common.serve
root = Path(os.environ["LITETUI_DATA_ROOT"])
initial_system = "CURRENT SYSTEM ROW, separate from agent files\n" + "Policy for this conversation."
initial_memory = "M" * 4094 + "雪\n\x1b[31mnot terminal color\x1b[0m\n" + "Second page line\n" * 275
changed_memory = "CHANGED memory 雪\nFresh snapshot after explicit refresh."
names = ("memory.md", "soul.md", "handoff.md")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def files(directory):
    return {
        name: digest((directory / name).read_bytes()) if (directory / name).is_file() else None
        for name in names
    }


def seed(app):
    from litetui import store_identity
    from litetui.lifecycle_hooks import HookConfig

    original_seed(app)
    app.hook_config = HookConfig(root / "inspection-hooks.json", root / "empty-project-hooks.json")
    os.environ["LITETUI_HOOKS"] = "on"
    app.hook_config.global_path.write_text(json.dumps({"version": 1, "hooks": [{
        "id": "audit-review", "events": ["tool_before"], "enabled": False,
        "executable": "private-test-executable", "env": {"TEST": "private-test-value"},
    }]}), encoding="utf-8")
    directory = app._agent_session.memory_root
    (directory / "memory.md").write_bytes(initial_memory.encode("utf-8"))
    (directory / "soul.md").write_bytes(b"")
    (directory / "handoff.md").unlink(missing_ok=True)
    app.conversation[0]["content"] = initial_system
    app.store.record_snapshot(app.conversation, store_identity=store_identity.capture(app), store_injected=True)
    assert not app.store.persist_error
    common.report["context_before"] = files(directory)
    (root / "context-seed.json").write_text(json.dumps({
        "directory": str(directory),
        "initial_memory": initial_memory,
        "changed_memory": changed_memory,
        "system": initial_system,
        "system_session": app.convo_id,
        "initial_digest": digest(initial_memory.encode("utf-8")),
        "changed_digest": digest(changed_memory.encode("utf-8")),
    }, ensure_ascii=False), encoding="utf-8")


async def serve(app):
    directory = app._agent_session.memory_root
    try:
        await original_serve(app)
    finally:
        (root / "context-after.json").write_text(json.dumps(files(directory)), encoding="utf-8")


common.seed = seed
common.history.serve = serve

if __name__ == "__main__":
    common.history.main()
