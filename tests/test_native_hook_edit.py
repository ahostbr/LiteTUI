"""Hook authoring through the canonical validator and writer, against private temp scope files."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from litetui import lifecycle_hooks as hooks
from litetui import native_hook_edit as edit
from litetui import native_hook_toggle as toggle

FORM = {"id": "audit", "events": ["tool_before"], "executable": "audit.exe", "argv": ["--quiet", "two words"],
        "enabled": True, "mode": "observe", "timeout": 5, "sources": [], "tools": ["read*"]}


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(root))
    monkeypatch.setenv("LITETUI_HOOKS", "on")
    config = hooks.HookConfig(*hooks.config_paths(workspace))
    app = SimpleNamespace(hook_config=config, _hook_workspace=workspace, _validate_owned_identity=lambda: None)
    bound = {"session_id": "session", "agent_id": "agent", "backend": "custom", "model": "unit"}
    bridge = SimpleNamespace(active=True, identity=bound, instance="work", closing=False, check=lambda: None)
    monkeypatch.setattr(edit.native_context.work, "bridge", lambda _: bridge)
    monkeypatch.setattr(edit.gui_rpc, "_idle", lambda _: None)

    async def forbidden(*args, **kwargs):
        pytest.fail("Authoring must never run a hook")

    monkeypatch.setattr(hooks, "run_hook", forbidden)
    ordinal = 0

    def request(verb, scope, **fields):
        nonlocal ordinal
        ordinal += 1
        cmd = {"type": edit.PREFIX + verb, "id": f"operation-{ordinal}", "version": 1, **bound,
               "work_instance": bridge.instance, "scope": scope, **fields}
        return edit.dispatch(app, cmd)

    def propose(scope, action, hook_id=None, hook=None):
        listing = request("snapshot", scope)
        return request("review", scope, revision=listing["revision"], action=action, hook_id=hook_id, hook=hook)

    def commit(scope, wire):
        applied = request("apply", scope, token=wire["token"])
        return request("readback", scope, token=wire["token"], apply_id=applied["apply_id"])

    return app, bridge, request, propose, commit


def canonical(rows):
    return (json.dumps({"version": 1, "hooks": rows}, indent=2) + "\n").encode()


def stored(**changes):
    return hooks.Hook.parse({**FORM, "cwd": None, "env": {}, **changes}).document()


def test_create_writes_canonical_document_then_independent_readback_matches(source):
    app, _, request, propose, commit = source
    snapshot = request("snapshot", "project")
    assert snapshot["rows"] == [] and snapshot["events"] == list(hooks.EVENTS)
    assert snapshot["gate_events"] == ["prompt_before", "tool_before", "completion_before"]
    assert not app.hook_config.project_path.exists()
    wire = propose("project", "create", None, FORM)
    assert wire["created_file"] and wire["before"] is None and wire["after"]["id"] == "audit"
    assert wire["file"] == "project .litetui/hooks.json" and wire["before_sha256"] is None
    assert wire["after_effective"] == {"scope": "project", "enabled": True, "command_id": wire["after_effective"]["command_id"]}
    assert not app.hook_config.project_path.exists(), "review must not write"
    saved = commit("project", wire)
    assert app.hook_config.project_path.read_bytes().replace(b"\r\n", b"\n") == canonical([stored()])
    assert saved["saved"] == wire["after"] and saved["effective"] == wire["after_effective"]
    assert saved["sha256"] == wire["after_sha256"] and saved["revision"] == wire["after_revision"]
    assert not app.hook_config.global_path.exists()


def test_update_preserves_private_environment_and_working_directory_and_other_hooks(source):
    app, _, request, propose, commit = source
    other = {"id": "other", "events": ["app_start"], "executable": "o.exe"}
    private = {"id": "audit", "events": ["tool_before"], "executable": "old.exe", "argv": ["x"],
               "env": {"SECRET_KEY": "SECRET_VALUE"}, "cwd": "private-dir"}
    app.hook_config.global_path.write_text(json.dumps({"version": 1, "hooks": [private, other]}), encoding="utf-8")
    listing = request("snapshot", "global")
    assert [r["env_count"] for r in listing["rows"]] == [1, 0] and listing["rows"][0]["custom_cwd"] is True
    assert "SECRET" not in json.dumps(listing) and "private-dir" not in json.dumps(listing)
    wire = propose("global", "update", "audit", {**FORM, "executable": "new.exe", "mode": "gate", "timeout": 2.5})
    assert wire["before"]["executable"] == "old.exe" and wire["after"]["executable"] == "new.exe"
    assert wire["after"]["env_count"] == 1 and wire["others"] == 1
    assert "SECRET" not in json.dumps(wire) and "private-dir" not in json.dumps(wire)
    saved = commit("global", wire)
    assert "SECRET" not in json.dumps(saved)
    rows = [h.document() for h in app.hook_config.read("global")]
    assert rows[0]["env"] == {"SECRET_KEY": "SECRET_VALUE"} and rows[0]["cwd"] == "private-dir"
    assert rows[0]["mode"] == "gate" and rows[0]["timeout"] == 2.5 and rows[1]["id"] == "other"


def test_delete_reveals_the_shadowed_global_hook_in_effective_readback(source):
    app, _, _, propose, commit = source
    row = {"id": "audit", "events": ["tool_before"], "executable": "g.exe"}
    app.hook_config.global_path.write_text(json.dumps({"version": 1, "hooks": [row]}), encoding="utf-8")
    app.hook_config.project_path.parent.mkdir(exist_ok=True)
    app.hook_config.project_path.write_text(json.dumps({"version": 1, "hooks": [{**row, "executable": "p.exe"}]}), encoding="utf-8")
    wire = propose("project", "delete", "audit")
    assert wire["after"] is None and wire["before_effective"]["scope"] == "project"
    assert wire["after_effective"]["scope"] == "global"
    saved = commit("project", wire)
    assert saved["saved"] is None and saved["effective"]["scope"] == "global"
    assert app.hook_config.read("project") == ()
    assert [h.id for h in app.hook_config.read("global")] == ["audit"]


@pytest.mark.parametrize(("change", "message"), [
    ({"timeout": 0}, "timeout"), ({"timeout": True}, "timeout"), ({"events": []}, "event"),
    ({"events": ["bogus"]}, "event"), ({"mode": "gate", "events": ["app_start"]}, "gate mode"), ({"mode": "x"}, "gate mode"),
    ({"sources": ["nowhere"]}, "source"), ({"executable": "  "}, "required"), ({"enabled": "yes"}, "enabled"),
    ({"id": "bad id"}, "IDs use"), ({"id": ""}, "IDs use"), ({"argv": [1]}, "limits"),
    ({"argv": ["x"] * 65}, "limits"), ({"executable": "x" * 1025}, "limits"),
])
def test_canonical_validator_refuses_and_nothing_is_written(source, change, message):
    app, bridge, request, propose, _ = source
    with pytest.raises(ValueError, match=message):
        propose("project", "create", None, {**FORM, **change})
    assert not app.hook_config.project_path.exists() and getattr(bridge, "hook_edit", None) is None
    with pytest.raises(ValueError, match="belongs to another"):
        request("apply", "project", token="0" * 64)


def test_form_must_carry_exactly_the_bounded_fields(source):
    _, _, _, propose, _ = source
    for form in ({**FORM, "env": {"A": "B"}}, {k: v for k, v in FORM.items() if k != "tools"}, [], None):
        with pytest.raises(ValueError, match="malformed"):
            propose("project", "create", None, form)


def test_duplicate_rename_missing_and_oversized_scope_are_refused(source):
    app, _, _, propose, commit = source
    commit("project", propose("project", "create", None, FORM))
    with pytest.raises(ValueError, match="already exists"):
        propose("project", "create", None, FORM)
    with pytest.raises(ValueError, match="no longer present"):
        propose("project", "update", "audit", {**FORM, "id": "renamed"})
    with pytest.raises(ValueError, match="no longer present"):
        propose("project", "update", "ghost", {**FORM, "id": "ghost"})
    with pytest.raises(ValueError, match="present hook"):
        propose("project", "delete", "ghost")
    with pytest.raises(ValueError, match="present hook"):
        propose("project", "delete", "audit", FORM)
    with pytest.raises(ValueError, match="Choose"):
        propose("project", "rename", "audit", FORM)
    rows = [{"id": f"h{i}", "events": ["app_start"], "executable": "x"} for i in range(64)]
    app.hook_config.global_path.write_text(json.dumps({"version": 1, "hooks": rows}), encoding="utf-8")
    with pytest.raises(ValueError, match="at most 64"):
        propose("global", "create", None, {**FORM, "id": "one-too-many"})


def test_stale_revision_refuses_review_and_apply_without_writing(source):
    app, _, request, propose, _ = source
    listing = request("snapshot", "project")
    app.hook_config.global_path.write_text(json.dumps({"version": 1, "hooks": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="changed since"):
        request("review", "project", revision=listing["revision"], action="create", hook_id=None, hook=FORM)
    for changed in ("project", "global"):
        wire = propose("project", "create", None, FORM)
        target = app.hook_config.path(changed)
        target.parent.mkdir(exist_ok=True)
        target.write_text(json.dumps({"version": 1, "hooks": []}, indent=4 if changed == "global" else 1), encoding="utf-8")
        before = target.read_bytes()
        with pytest.raises(ValueError, match="changed"):
            request("apply", "project", token=wire["token"])
        assert target.read_bytes() == before
        target.unlink()


def test_apply_checks_revision_again_after_the_canonical_locks_are_held(source, monkeypatch):
    app, _, request, propose, _ = source
    app.hook_config.project_path.parent.mkdir(exist_ok=True)
    wire = propose("project", "create", None, FORM)
    real = hooks._file_lock
    held = []

    @contextmanager
    def interfering(path, **kwargs):
        with real(path, **kwargs):
            held.append(path)
            if path == app.hook_config.project_path:
                assert held == [app.hook_config.global_path, app.hook_config.project_path]
                path.write_text(json.dumps({"version": 1, "hooks": []}), encoding="utf-8")
            try:
                yield
            finally:
                held.pop()

    monkeypatch.setattr(hooks, "_file_lock", interfering)
    with pytest.raises(ValueError, match="changed"):
        request("apply", "project", token=wire["token"])
    assert json.loads(app.hook_config.project_path.read_text())["hooks"] == [] and not held


def test_foreign_lock_declines_and_double_apply_and_other_identity_are_refused(source, monkeypatch):
    app, bridge, request, propose, _commit = source
    wire = propose("project", "create", None, FORM)
    app.hook_config.project_path.parent.mkdir(exist_ok=True)
    with hooks._file_lock(app.hook_config.project_path), pytest.raises(ValueError, match="locked"):
        request("apply", "project", token=wire["token"])
    assert not app.hook_config.project_path.exists()
    applied = request("apply", "project", token=wire["token"])
    with pytest.raises(ValueError, match="already applied"):
        request("apply", "project", token=wire["token"])
    with pytest.raises(ValueError, match="another application"):
        request("readback", "project", token=wire["token"], apply_id="forged")
    with pytest.raises(ValueError, match="belongs to another"):
        request("apply", "global", token=wire["token"])
    bridge.instance = "other-work"
    with pytest.raises(ValueError):
        request("readback", "project", token=wire["token"], apply_id=applied["apply_id"])


def test_readback_rejects_a_change_after_publication(source):
    app, _, request, propose, _ = source
    wire = propose("project", "create", None, FORM)
    applied = request("apply", "project", token=wire["token"])
    app.hook_config.global_path.write_bytes(canonical([]))
    with pytest.raises(ValueError, match="unverified"):
        request("readback", "project", token=wire["token"], apply_id=applied["apply_id"])


def test_hooks_switched_off_is_reported_and_malformed_scope_blocks_editing(source, monkeypatch):
    app, _, request, propose, commit = source
    monkeypatch.setenv("LITETUI_HOOKS", "off")
    wire = propose("project", "create", None, FORM)
    assert wire["hooks_off"] and wire["after_effective"] is None
    assert commit("project", wire)["effective"] is None
    monkeypatch.setenv("LITETUI_HOOKS", "on")
    app.hook_config.global_path.write_text('{"version": 1, "hooks": [{"SECRET_FIELD": 1}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="no edit is available") as exc:
        request("snapshot", "project")
    assert "SECRET_FIELD" not in str(exc.value)


def test_registered_with_the_gui_surface():
    from litetui import gui_rpc

    for verb in ("snapshot", "review", "apply", "readback"):
        assert f"hooks.native_edit_{verb}" in gui_rpc.OPERATIONS


BROKEN = '{"version": 1, "hooks": [{"id": "keep", "events": ["app_start"], "executable": "x.exe", "bogus": 1, "env": {"K": "V"}}]}'
FIXED = '{"version": 1, "hooks": [{"id": "keep", "events": ["app_start"], "executable": "x.exe", "env": {"K": "V"}}]}'


@pytest.fixture
def mending(source):
    app, bridge, request, propose, commit = source
    ordinal = 0

    def mend(verb, scope="project", **fields):
        nonlocal ordinal
        ordinal += 1
        return edit.dispatch(app, {"type": edit.REPAIR + verb, "id": f"repair-{ordinal}", "version": 1,
                                   **bridge.identity, "work_instance": bridge.instance, "scope": scope, **fields})

    return app, bridge, request, propose, commit, mend


def test_unreadable_scope_snapshot_offers_the_stored_text_and_the_canonical_diagnostic(source):
    app, _, request, _, _ = source
    app.hook_config.project_path.write_text(BROKEN, encoding="utf-8")
    shot = request("snapshot", "project")
    assert shot["state"] == "invalid" and shot["rows"] == [] and shot["text"] == BROKEN
    assert "unknown hook fields: bogus" in shot["error"] and shot["revision"] == toggle.digest(BROKEN.encode())
    app.hook_config.project_path.write_bytes(b"x" * (edit.REPAIR_LIMIT + 1))
    with pytest.raises(ValueError, match="64 KiB"):
        request("snapshot", "project")
    app.hook_config.project_path.write_bytes(b"\xff\xfe not json")
    assert request("snapshot", "project")["state"] == "invalid"


def test_repair_replaces_the_exact_unreadable_bytes_after_canonical_validation(mending):
    app, _, request, _, _, mend = mending
    app.hook_config.project_path.write_text(BROKEN, encoding="utf-8")
    revision = request("snapshot", "project")["revision"]
    wire = mend("review", revision=revision, text=FIXED)
    assert wire["before_sha256"] == revision and wire["before_bytes"] == len(BROKEN)
    assert json.loads(wire["after_text"])["hooks"][0]["env"] == {"K": "V"}
    assert wire["rows"][0]["env_count"] == 1 and '"V"' not in json.dumps(wire["rows"])
    assert app.hook_config.project_path.read_text() == BROKEN, "review must not write"
    applied = mend("apply", token=wire["token"])
    saved = mend("readback", token=wire["token"], apply_id=applied["apply_id"])
    assert saved["sha256"] == wire["after_sha256"] and saved["rows"] == wire["rows"]
    assert [h.id for h in app.hook_config.read("project")] == ["keep"]
    with pytest.raises(ValueError, match="already applied"):
        mend("apply", token=wire["token"])
    assert request("snapshot", "project")["state"] == "ok"


@pytest.mark.parametrize("text", [
    "{", '{"version": 1, "hooks": [{"id": "keep"}]}', '{"version": 2, "hooks": []}',
    '{"version": 1, "hooks": [{"id": "bad id", "events": ["app_start"], "executable": "x"}]}'])
def test_repair_refuses_text_the_canonical_validator_refuses(mending, text):
    app, _, request, _, _, mend = mending
    app.hook_config.project_path.write_text(BROKEN, encoding="utf-8")
    with pytest.raises(ValueError):
        mend("review", revision=request("snapshot", "project")["revision"], text=text)
    assert app.hook_config.project_path.read_text() == BROKEN


def test_repair_is_refused_for_readable_stale_or_swapped_documents(mending):
    app, bridge, _, propose, commit, mend = mending
    commit("project", propose("project", "create", None, FORM))
    digest = toggle.digest(app.hook_config.project_path.read_bytes())
    with pytest.raises(ValueError, match="no unreadable"):
        mend("review", revision=digest, text=FIXED)
    app.hook_config.project_path.write_text(BROKEN, encoding="utf-8")
    with pytest.raises(ValueError, match="changed since"):
        mend("review", revision=digest, text=FIXED)
    wire = mend("review", revision=toggle.digest(BROKEN.encode()), text=FIXED)
    app.hook_config.project_path.write_text(BROKEN + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        mend("apply", token=wire["token"])
    assert app.hook_config.project_path.read_text() == BROKEN + " "
    with pytest.raises(ValueError, match="belongs to another"):
        mend("apply", token="0" * 64)
    with pytest.raises(ValueError, match="belongs to another"):
        edit.dispatch(app, {"type": edit.PREFIX + "apply", "id": "x", "version": 1, **bridge.identity,
                            "work_instance": bridge.instance, "scope": "project", "token": wire["token"]})


def test_repair_apply_is_a_compare_and_swap_under_the_canonical_lock(mending, monkeypatch):
    app, _, request, _, _, mend = mending
    app.hook_config.project_path.write_text(BROKEN, encoding="utf-8")
    wire = mend("review", revision=request("snapshot", "project")["revision"], text=FIXED)
    real = app.hook_config.repair

    def racing(scope, text, expected):
        app.hook_config.project_path.write_text(BROKEN, encoding="utf-8")  # same bytes, new file
        app.hook_config.project_path.write_text(BROKEN + "\n", encoding="utf-8")
        return real(scope, text, expected)

    monkeypatch.setattr(app.hook_config, "repair", racing)
    with pytest.raises(ValueError, match="repair conflict"):
        mend("apply", token=wire["token"])
    assert app.hook_config.project_path.read_text() == BROKEN + "\n"
