"""Disposition, byte preservation, binding and refusal proofs using private scope files."""
import json
import os
from types import SimpleNamespace

import pytest

from litetui import lifecycle_hooks as hooks
from litetui import native_hook_toggle as native


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(root))
    monkeypatch.setenv("LITETUI_HOOKS", "on")
    config = hooks.HookConfig(*hooks.config_paths(workspace))
    config.project_path.parent.mkdir(exist_ok=True)
    app = SimpleNamespace(hook_config=config, _hook_workspace=workspace, _validate_owned_identity=lambda: None)
    bound = {"session_id": "session", "agent_id": "agent", "backend": "custom", "model": "unit"}
    bridge = SimpleNamespace(active=True, identity=bound, instance="work", closing=False, check=lambda: None)
    monkeypatch.setattr(native.work, "bridge", lambda _: bridge)
    monkeypatch.setattr(native.gui_rpc, "_idle", lambda _: None)
    global_row = {"id": "overlap", "events": ["tool_before"], "executable": "global.exe",
                  "env": {"TOKEN": "global-hidden"}, "argv": ["secret argument"]}
    project = {"id": "overlap", "events": ["tool_before"], "executable": "project.exe", "enabled": False}
    original = b"\xef\xbb\xbf" + json.dumps({"version": 1, "hooks": [global_row]}, indent=3).encode() + b"\r\n"
    config.global_path.write_bytes(original)
    config.project_path.write_text(json.dumps({"version": 1, "hooks": [project]}), encoding="utf-8")
    ordinal = 0

    def request(verb, scope, **fields):
        nonlocal ordinal
        ordinal += 1
        cmd = {"type": native.PREFIX + verb, "id": f"operation-{ordinal}", "version": 1, **bound,
               "work_instance": bridge.instance, "scope": scope, **fields}
        return native.dispatch(app, cmd)

    return app, bridge, request


def review(request, scope):
    listing = request("list", scope)
    return request("review", scope, revision=listing["revision"], hook_id="overlap")


@pytest.mark.parametrize(("scope", "before", "after"), [
    ("global", "shadowed", "shadowed"), ("project", "disabled", "enabled"),
])
def test_toggles_exact_existing_bytes_and_independent_effective_readback(source, scope, before, after):
    app, _, request = source
    path = app.hook_config.path(scope)
    original = path.read_bytes()
    wire = review(request, scope)
    assert wire["before"]["effect"] == before and wire["after"]["effect"] == after
    assert all(secret not in json.dumps(wire) for secret in ("global-hidden", "secret argument", "TOKEN"))
    applied = request("apply", scope, token=wire["token"])
    saved = request("readback", scope, token=wire["token"], apply_id=applied["apply_id"])
    assert saved["actual"] == wire["after"] and saved["sha256"] == wire["after_sha256"]
    if scope == "project":
        assert path.read_bytes() == original.replace(b'"enabled": false', b'"enabled": true')
    else:
        start = original.index(b'{', original.index(b'"hooks"')) + 1
        assert path.read_bytes() == original[:start] + b'"enabled":false,' + original[start:]
    with pytest.raises(ValueError, match="already applied"):
        request("apply", scope, token=wire["token"])


@pytest.mark.parametrize("stale", ["selected", "other", "scope_path", "environment", "owner"])
def test_stale_refuses_without_mutating_either_scope(source, monkeypatch, stale):
    app, bridge, request = source
    config = app.hook_config
    wire = review(request, "global")
    if stale in {"selected", "other"}:
        path = config.global_path if stale == "selected" else config.project_path
        path.write_bytes(path.read_bytes() + b" ")
    elif stale == "scope_path":
        config.project_path = config.project_path.with_name("other.json")
    elif stale == "environment":
        monkeypatch.setenv("LITETUI_HOOKS", "off")
    else:
        bridge.instance = "new-work"
    before = config.global_path.read_bytes()
    with pytest.raises(ValueError):
        request("apply", "global", token=wire["token"])
    assert config.global_path.read_bytes() == before


def test_again_reads_disk_after_publish_and_rejects_unverified_result(source):
    app, _, request = source
    wire = review(request, "project")
    applied = request("apply", "project", token=wire["token"])
    app.hook_config.global_path.write_bytes(app.hook_config.global_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="unverified"):
        request("readback", "project", token=wire["token"], apply_id=applied["apply_id"])


def test_rejects_duplicates_links_and_private_unknown_fields_without_exporting_them(source):
    app, _, request = source
    path = app.hook_config.global_path
    original = path.read_bytes()
    for invalid in (b'{"version":1,"version":1,"hooks":[]}', original.replace(b'"executable"', b'"SECRET_NAME"')):
        path.write_bytes(invalid)
        with pytest.raises(ValueError, match="Unsupported") as exc:
            request("list", "global")
        assert "SECRET_NAME" not in str(exc.value)
    path.write_bytes(original)
    os.link(path, path.with_name("hard-link"))
    with pytest.raises(ValueError, match="unlinked"):
        request("list", "global")



def test_publication_holds_both_canonical_scope_locks_and_checks_after_acquiring(source, monkeypatch):
    from contextlib import contextmanager

    app, _, request = source
    config = app.hook_config
    wire = review(request, "project")
    original = config.project_path.read_bytes()
    real_lock = hooks._file_lock
    held = []

    @contextmanager
    def changed_while_locking(path, **kwargs):
        with real_lock(path, **kwargs):
            held.append(path)
            if path == config.project_path:
                assert held == [config.global_path, config.project_path]
                config.global_path.write_bytes(config.global_path.read_bytes() + b" ")
            try:
                yield
            finally:
                held.pop()

    monkeypatch.setattr(hooks, "_file_lock", changed_while_locking)
    with pytest.raises(ValueError, match="changed"):
        request("apply", "project", token=wire["token"])
    assert config.project_path.read_bytes() == original and not held



def test_foreign_scope_lock_declines_without_publishing(source):
    app, _, request = source
    config = app.hook_config
    wire = review(request, "project")
    before = config.project_path.read_bytes()
    with hooks._file_lock(config.global_path), pytest.raises(ValueError, match="locked"):
        request("apply", "project", token=wire["token"])
    assert config.project_path.read_bytes() == before
