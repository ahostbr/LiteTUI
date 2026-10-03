"""Publication and provenance edge cases at the existing T0338 seams."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from litetui import paths, prompt_commands as commands, prompt_overrides as prompts
from litetui.app import LiteTUI
from test_prompt_overrides_t0338 import command, layer
from test_prompt_commands_t0338 import owners, request


def test_metadata_escaped_envelope_and_bounded_decoded_body(tmp_path):
    text = "\x01" * prompts.MAX_BYTES
    row = {"version": 1, "active": False, "global_text": text,
           "global_sha": prompts.sha(text), "body_sha": prompts.sha("")}
    folder = tmp_path / "prompts"
    folder.mkdir()
    path = folder / "compact.md.json"
    path.write_text(json.dumps(row))
    assert path.stat().st_size > prompts.MAX_BYTES
    assert prompts.metadata(tmp_path, "compact.md")["global_text"] == text
    row["global_text"] += "x"
    row["global_sha"] = prompts.sha(row["global_text"])
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="metadata"):
        prompts.metadata(tmp_path, "compact.md")


@pytest.mark.parametrize("name,allowed", [("compact.md", True), ("compact.md.json", True),
                                         ("systemprompt.md", False), ("unknown.md", False)])
def test_retained_publication_temporary_is_not_active(tmp_path, name, allowed):
    folder = tmp_path / "prompts"
    folder.mkdir()
    (folder / f".{name}.0123456789abcdef.tmp").write_text("failed write")
    if allowed:
        prompts.validate_layer(tmp_path)
        assert paths.prompt_file("compact.md", convo_dir=tmp_path) == paths.prompt_file("compact.md")
    else:
        with pytest.raises(ValueError, match="Unrecognized"):
            prompts.validate_layer(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("nonce", ["../../outside", "a" * 31, "A" * 32])
async def test_attach_refuses_nonce_before_credential_access(monkeypatch, nonce):
    identity = str(uuid4())
    monkeypatch.setattr(commands, "registry", lambda _: {
        "agent_id": identity, "session_pid": 123,
        "litetui_rpc": {"agent_id": identity, "pid": 123, "host": "127.0.0.1",
                         "port": 4100, "nonce": nonce, "[redacted]": "/outside"}})
    with pytest.raises(ValueError, match="endpoint unavailable"):
        await commands.attach(identity, {})


@pytest.mark.parametrize("existing", [False, True])
def test_crash_between_manifest_and_body_fails_closed(tmp_path, monkeypatch, existing):
    if existing:
        commands.apply(tmp_path, command(tmp_path, "compact.md", "previous"))
    cmd = command(tmp_path, "compact.md", "next")
    real = commands.publish
    def broken(path, text):
        if path.name == "compact.md":
            raise OSError("injected body failure")
        real(path, text)
    monkeypatch.setattr(commands, "publish", broken)
    with pytest.raises(OSError, match="injected"):
        commands.apply(tmp_path, cmd)
    rows = [json.loads(line) for line in (tmp_path / "prompts" / "audit.jsonl").read_text().splitlines()]
    assert rows[-1]["phase"] == "intent"
    with pytest.raises(ValueError, match="missing|disagrees"):
        paths.prompt_file("compact.md", convo_dir=tmp_path)
    with pytest.raises(ValueError, match="uncertain publication"):
        commands.apply(tmp_path, cmd)


def test_crash_after_body_before_applied_receipt_is_honestly_unknown(tmp_path, monkeypatch):
    cmd = command(tmp_path, "compact.md", "next")
    real = commands.append_audit
    def broken(directory, row):
        if row["phase"] == "applied":
            raise OSError("injected receipt failure")
        real(directory, row)
    monkeypatch.setattr(commands, "append_audit", broken)
    with pytest.raises(OSError):
        commands.apply(tmp_path, cmd)
    assert prompts.read_text(paths.prompt_file("compact.md", convo_dir=tmp_path)) == "next"
    with pytest.raises(ValueError, match="uncertain publication"):
        commands.apply(tmp_path, cmd)


def test_old_request_after_reset_does_not_claim_current_or_reapply(tmp_path):
    cmd = command(tmp_path, "compact.md", "old")
    commands.apply(tmp_path, cmd)
    commands.apply(tmp_path, {**cmd, "id": str(uuid4()), "action": "reset", "text": ""})
    result, _ = commands.apply(tmp_path, cmd)
    assert result["duplicate"] and result["current_revision"] is False
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN"
    assert paths.prompt_file("compact.md", convo_dir=tmp_path) == paths.prompt_file("compact.md")


def test_managed_body_tamper_refused(tmp_path):
    cmd = command(tmp_path, "compact.md", "original")
    commands.apply(tmp_path, cmd)
    layer(tmp_path, "compact.md", "untracked change")
    with pytest.raises(ValueError, match="disagrees"):
        paths.prompt_file("compact.md", convo_dir=tmp_path)
    with pytest.raises(ValueError, match="disagrees"):
        commands.apply(tmp_path, cmd)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign-session", "pending", "loading", "store-dir", "authority", "sha"])
async def test_owner_and_schema_edges_refuse_before_writes(owners, fault):
    leader, worker = owners
    cmd = {**request(worker), "type": commands.APPLY, "actor": leader.seat.agent_id,
           "notification_sent": None}
    if fault == "foreign-session":
        worker.store.agent_session = leader._agent_session
    elif fault in {"pending", "loading"}:
        setattr(worker.store, fault, True)
    elif fault == "store-dir":
        worker.store.convo_dir = leader.convo_dir
    elif fault == "authority":
        worker.seat.name = "Other"
    else:
        cmd["global_sha"] = "g" * 64
    with pytest.raises(ValueError):
        await commands.dispatch(worker, cmd)
    assert not (worker.convo_dir / "prompts").exists()


def test_host_rebuild_wire_keys_and_custom_legacy_guidance(tmp_path):
    app = LiteTUI()
    app._materialise_convo()
    layer(app.convo_dir, "ordinary-instructions.md", "current ordinary")
    prompts.refresh(app)
    for tools in (False, True):
        app.tools_enabled = tools
        app.conversation[0] = app._host_prompt_message()
        wire = app._request_messages()
        assert "current ordinary" in wire[0]["content"]
        assert not any(k.startswith("litetui_") for k in wire[0])
    custom = "custom legacy safety must remain"
    app.conversation[0] = {"role": "system", "content": custom}
    wire = app._request_messages()
    assert wire[0]["content"].startswith(custom)
    assert "host supplied, not user-authored" in wire[0]["content"]
    assert "current ordinary" in wire[0]["content"]
    assert len(app.conversation) == len(wire)
    commands.apply(app.convo_dir, command(app.convo_dir, action="reset", text=""))
    prompts.refresh(app)
    assert "## Engineering practices" in app._request_messages()[0]["content"]


def test_empty_ordinary_host_boundary_is_reloadable():
    app = LiteTUI()
    app._materialise_convo()
    layer(app.convo_dir, "ordinary-instructions.md", "")
    prompts.refresh(app)
    app.conversation[0] = app._host_prompt_message()
    assert "Shared-worktree caveat" in app._request_messages()[0]["content"]
    layer(app.convo_dir, "ordinary-instructions.md", "restored ordinary")
    prompts.refresh(app)
    assert "restored ordinary\n\n" in app._request_messages()[0]["content"]


def test_seat_text_is_removed_at_owned_trailing_boundary_only():
    app = LiteTUI()
    app._materialise_convo()
    seat = "SAME SEAT"
    ordinary = "ordinary quotes\n\n[LiteTUI seat instructions]\n" + seat
    layer(app.convo_dir, "ordinary-instructions.md", ordinary)
    layer(app.convo_dir, "seat-instructions.md", seat)
    prompts.refresh(app)
    app.conversation[0] = app._host_prompt_message()
    layer(app.convo_dir, "seat-instructions.md", "NEXT SEAT")
    prompts.refresh(app)
    result = app._request_messages()[0]["content"]
    assert ordinary in result and "NEXT SEAT" in result
    assert result.count(seat) == 1


def test_invalid_managed_revision_holds_actual_admission_without_floor_change(monkeypatch):
    from litetui import hook_host
    app = LiteTUI()
    app._materialise_convo()
    before = app.conversation[0]["content"]
    notices = []
    app._system = notices.append
    commands.apply(app.convo_dir, command(app.convo_dir, "compact.md", "managed"))
    layer(app.convo_dir, "compact.md", "mismatch")
    monkeypatch.setattr(hook_host, "refuse_below_floor", lambda *args: False)
    assert hook_host.accept_prompt(app, {"content": "never sent", "source": "typed"}) is False
    assert app.conversation[0]["content"] == before
    assert not any(m.get("content") == "never sent" for m in app.conversation)
    assert "input held unsent" in notices[-1]


def test_snapshot_spanning_publication_refuses_mixed_pair(tmp_path, monkeypatch):
    old = command(tmp_path, "compact.md", "old")
    commands.apply(tmp_path, old)
    new = command(tmp_path, "compact.md", "new")
    read = prompts.read_text
    reads = 0
    def racing(path, **kwargs):
        nonlocal reads
        text = read(path, **kwargs)
        if path.name == "compact.md" and path.parent == tmp_path / "prompts":
            reads += 1
            # The first read validates resolution before signature capture;
            # the second is the snapshot body read spanning the publication.
            if reads == 2:
                commands.apply(tmp_path, new)
        return text
    monkeypatch.setattr(prompts, "read_text", racing)
    with pytest.raises(ValueError, match="snapshot|publication"):
        prompts.refresh(SimpleNamespace(convo_dir=tmp_path))


@pytest.mark.parametrize("tail", ['{"phase":', 'not-json\n', '[]\n'])
def test_torn_or_malformed_audit_preserved_refused(tmp_path, tail):
    folder = tmp_path / "prompts"
    folder.mkdir()
    audit = folder / "audit.jsonl"
    audit.write_text(tail)
    with pytest.raises(ValueError, match="audit"):
        commands.apply(tmp_path, command(tmp_path, "compact.md", "new"))
    assert audit.read_text() == tail
    assert not (folder / "compact.md.json").exists()


def test_orphan_retention_is_specific_preserved_outcome(tmp_path, monkeypatch):
    cmd = command(tmp_path, "compact.md", "new")
    def fail(*args):
        raise OSError("intent failed")
    monkeypatch.setattr(commands, "append_audit", fail)
    with pytest.raises(OSError):
        commands.apply(tmp_path, cmd)
    with pytest.raises(ValueError, match="without audit intent"):
        commands.apply(tmp_path, cmd)
    assert not (tmp_path / "prompts" / "compact.md.json").exists()


@pytest.mark.asyncio
async def test_local_notice_failure_preserves_applied_result(owners):
    leader, worker = owners
    def fail(*args):
        raise OSError("UI unavailable")
    worker._system = fail
    cmd = {**request(worker), "type": commands.APPLY, "actor": leader.seat.agent_id,
           "notification_sent": None}
    result = await commands.dispatch(worker, cmd)
    assert result["status"] == "APPLIED_NOTIFICATION_UNKNOWN" and result["notification_sent"] is None
    assert result["local_notice_failed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, "1", True, 2])
async def test_quoted_marker_absent_invalid_policy_remains_legacy(owners, version):
    from litetui.claude_persistence import ClaudeLedger
    leader, worker = owners
    worker.backend.name = "claude"
    ledger = worker._claude_ledger = ClaudeLedger(worker.convo_dir)
    segment = ledger.select_segment(str(worker.convo_dir))
    ledger.fix_system_prompt(segment["id"], 'legacy quoted "mutable-policy version 1" ordinary')
    if version is not None:
        ledger._data["segments"][segment["id"]]["host_policy_version"] = version
        ledger._save()
    cmd = {**request(worker), "type": commands.APPLY, "actor": leader.seat.agent_id,
           "notification_sent": None}
    with pytest.raises(ValueError, match="one /claude new"):
        await commands.dispatch(worker, cmd)
    assert not (worker.convo_dir / "prompts").exists()


def test_policy_provenance_saved_resumed_not_retrofitted(tmp_path):
    from litetui.claude_persistence import ClaudeLedger
    ledger = ClaudeLedger(tmp_path)
    old = ledger.select_segment(str(tmp_path))
    ledger.fix_system_prompt(old["id"], "old bytes")
    assert ledger.fix_system_prompt(old["id"], "new floor", host_policy_version=1) == "old bytes"
    assert "host_policy_version" not in ledger.selected
    fresh = ledger.select_segment(str(tmp_path), new=True)
    ledger.fix_system_prompt(fresh["id"], "fresh floor", host_policy_version=1)
    ledger.bind_session(fresh["id"], "native-new")
    resumed = ClaudeLedger(tmp_path).selected
    assert resumed["host_policy_version"] == 1 and resumed["session_id"] == "native-new"
    assert resumed["system_prompt"] == "fresh floor"
    assert ledger.segment(old["id"])["system_prompt"] == "old bytes"
