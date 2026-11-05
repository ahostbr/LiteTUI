"""Owned history reads use real storage and controlled external boundaries."""
from __future__ import annotations

import base64
import copy
import json
import secrets
import uuid

import pytest
from test_rust_backend_transition import disk
from test_rust_backend_transition import switch_app as _controlled_app

from litetui import conversation_page, native_history
from litetui.launch_options import LaunchOptions

switch_app = _controlled_app
pytestmark = pytest.mark.asyncio


def common(app, **kwargs):
    if app._launch_options is None:
        app._launch_options = LaunchOptions()
    app._launch_options.history_projection_version = 1
    return {"version": 1, "from_session_id": app.convo_id, "session_id": app.convo_id,
            "agent_id": app._agent_session.authority.agent_id, "backend": app.backend.name,
            "model": app.model_id, "scope": "current", "catalog_snapshot": None, **kwargs}


def save_history(app, messages):
    app.conversation = messages
    app._snapshot("controlled history fixture")


def read_page(app, command, descriptor, cursor=None, anchor="head"):
    args = {**command, "snapshot": descriptor["snapshot"], "cursor": cursor}
    if cursor is None:
        args["anchor"] = anchor
    return native_history.page(app, args)


async def test_owned_snapshot_and_every_page_are_pure_and_lossless(switch_app):
    app = switch_app
    save_history(app, [{"role": "system", "content": "source"}] + [{"role": "user", "content": str(i)} for i in range(1201)])
    command = common(app)
    before = disk(app)
    context = copy.deepcopy(app.conversation)
    snapshot = native_history.snapshot(app, command)
    assert snapshot["counts"]["messages"] == 1202 and not snapshot["fresh_system_context"]
    page = read_page(app, command, snapshot)
    values = [row["text"] for row in page["fragments"]]
    while page["next_cursor"] is not None:
        page = read_page(app, command, snapshot, page["next_cursor"])
        values.extend(row["text"] for row in page["fragments"])
    assert values == [str(i) for i in range(1201)]
    assert disk(app) == before and app.conversation == context
    tail = read_page(app, command, snapshot, anchor="tail")
    assert tail["at_end"] and tail["fragments"][-1]["text"] == "1200"


async def test_stale_snapshot_after_context_edit_refuses_without_rebinding(switch_app):
    app = switch_app
    command = common(app)
    snapshot = native_history.snapshot(app, command)
    save_history(app, [{"role": "system", "content": "changed"}])
    before = disk(app)
    with pytest.raises(ValueError, match="History changed"):
        read_page(app, command, snapshot)
    assert disk(app) == before


async def test_live_disk_disagreement_refuses_without_repair(switch_app):
    app = switch_app
    command = common(app)
    before = disk(app)
    app.conversation.append({"role": "user", "content": "not saved"})
    with pytest.raises(ValueError, match="differs"):
        native_history.snapshot(app, command)
    assert disk(app) == before


async def test_forged_and_previous_runtime_cursors_refuse(switch_app):
    app = switch_app
    save_history(app, [{"role": "user", "content": "x" * 20000}])
    command = common(app)
    snapshot = native_history.snapshot(app, command)
    cursor = read_page(app, command, snapshot)["next_cursor"]
    assert cursor is not None
    payload, signature = cursor.split(".")
    value = json.loads(base64.urlsafe_b64decode(payload))
    value["utf8_offset"] += 1
    forged = base64.urlsafe_b64encode(json.dumps(value).encode()).decode() + "." + signature
    with pytest.raises(ValueError):
        read_page(app, command, snapshot, forged)
    app._native_history_secret = secrets.token_bytes(32)
    current = native_history.snapshot(app, command)
    assert current["snapshot"] != snapshot["snapshot"]
    with pytest.raises(ValueError):
        read_page(app, command, current, cursor)


async def test_preview_binds_catalog_and_refuses_old_catalog_after_write(switch_app):
    app = switch_app
    target = str(uuid.uuid4())
    directory = app._agent_session.conversation_directory(target)
    directory.mkdir()
    path = directory / "convo.jsonl"
    records = [{"type": "meta", "id": target}, {"type": "msg", "message": {"role": "user", "content": "other"}}]
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    catalog = conversation_page._catalog(app, app.convo_id)[4]
    command = common(app, session_id=target, scope="preview", catalog_snapshot=catalog)
    before = disk(app)
    snapshot = native_history.snapshot(app, command)
    assert snapshot["from_session_id"] == app.convo_id and snapshot["session_id"] == target
    assert read_page(app, command, snapshot)["fragments"][0]["text"] == "other"
    assert disk(app) == before
    path.write_text(path.read_text(encoding="utf-8") + json.dumps({"type": "rename", "name": "changed"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="catalog changed"):
        native_history.snapshot(app, command)


async def test_canonical_capture_replays_edits_truncate_rename_and_summaries():
    rows = [{"type": "meta", "id": "id"}, {"type": "snapshot", "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "old"}]},
            {"type": "edit", "index": 1, "message": {"role": "user", "content": "edited"}},
            {"type": "truncate", "keep_from": 1, "keep_system": True}, {"type": "rename", "name": "title"},
            {"type": "card_summary", "key": "answer", "summary": "saved"}]
    capture = native_history.capture_bytes(("\n".join(json.dumps(row) for row in rows) + '\n{"torn"').encode())
    assert capture.meta["name"] == "title"
    assert capture.messages == [{"role": "system", "content": "s"}, {"role": "user", "content": "edited"}]
    assert capture.summaries == {"answer": "saved"}


async def test_source_file_limit_is_checked_before_canonical_parse(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Canonical parse must not run on oversized input")
    monkeypatch.setattr(native_history.ConversationRepository, "read", forbidden)
    with pytest.raises(ValueError, match="file budget"):
        native_history.capture_bytes(b"x" * (native_history.MAX_TRANSCRIPT + 1))


async def test_history_operation_requires_early_explicit_opt_in(switch_app):
    app = switch_app
    command = common(app)
    app._launch_options.history_projection_version = None
    with pytest.raises(ValueError, match="Restart"):
        native_history.snapshot(app, command)


async def test_index_cache_reuses_only_exact_revision_and_still_checks_disk(switch_app, monkeypatch):
    app = switch_app
    save_history(app, [{"role": "user", "content": "x" * 20000}])
    command = common(app)
    calls = []
    original = native_history.projection.project

    def project(messages, summaries):
        calls.append(copy.deepcopy(messages))
        return original(messages, summaries)

    monkeypatch.setattr(native_history.projection, "project", project)
    descriptor = native_history.snapshot(app, command)
    cache = app._native_history_projection_cache
    page = read_page(app, command, descriptor)
    while page["next_cursor"]:
        page = read_page(app, command, descriptor, page["next_cursor"])
    assert len(calls) == 1 and app._native_history_projection_cache is cache
    with app.convo_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "rename", "name": "changed outside the page"}) + "\n")
    with pytest.raises(ValueError, match="History changed"):
        read_page(app, command, descriptor)
    assert len(calls) == 2 and app._native_history_projection_cache is not cache
    after = native_history.snapshot(app, command)
    assert after["context_sha256"] == descriptor["context_sha256"]
    assert after["transcript_revision"] != descriptor["transcript_revision"]
    assert after["snapshot"] != descriptor["snapshot"]


async def test_tracked_worker_admission_is_read_only_and_refuses_replacements(switch_app, monkeypatch):
    import asyncio
    from contextlib import suppress
    from types import SimpleNamespace

    from litetui import conversation_transition

    app = switch_app
    common(app)
    owner = asyncio.create_task(asyncio.Event().wait())
    connection = SimpleNamespace(is_running=True)
    history = SimpleNamespace(is_running=True)
    current = [connection]
    monkeypatch.setattr("textual.worker.get_current_worker", lambda: current[0])
    app._conversation_transition_task = owner
    app._conversation_transition_pending = True
    app._native_history_open_capture = native_history.capture_file(app.convo_path)
    app._resume_connect_worker = connection
    app._native_history_worker = history
    app._native_history_open_workers = [connection]
    app._native_history_open_roles = native_history._WorkerRoles(
        app._native_history_open_capture, owner, connection)
    try:
        assert native_history.connection_reader(app)
        conversation_transition.require_open(app)
        with pytest.raises(ValueError, match="transition"):
            conversation_transition.require_execution(app)
        current[0] = history
        assert not native_history.connection_reader(app)
        with pytest.raises(ValueError, match="transition"):
            conversation_transition.require_open(app)
        current[0] = connection
        native_history.track_history_worker(app, history)
        current[0] = history
        assert native_history.connection_reader(app)
        conversation_transition.require_open(app)
        with pytest.raises(ValueError, match="transition"):
            conversation_transition.require_execution(app)
        replacement = SimpleNamespace(is_running=True)
        app._native_history_worker = replacement
        assert not native_history.connection_reader(app)
        current[0] = replacement
        assert not native_history.connection_reader(app)
        with pytest.raises(ValueError, match="captured connection"):
            native_history.track_history_worker(app, replacement)
        current[0] = connection
        app._conversation_transition_shutdown_requested = True
        assert not native_history.connection_reader(app)
        with pytest.raises(ValueError, match="shut|closing"):
            conversation_transition.require_open(app)
        with pytest.raises(ValueError, match="already bound"):
            native_history.track_history_worker(app, replacement)
        assert app._native_history_open_workers[-1] is replacement
        assert app._native_history_open_roles.history is history
        descendant = SimpleNamespace(is_running=True)
        assert native_history.track_connection_worker(app, descendant)
        current[0] = descendant
        assert not native_history.connection_reader(app)
        with pytest.raises(ValueError, match="captured connection"):
            native_history.track_connection_worker(app, SimpleNamespace(is_running=True))
        current[0] = connection
        app._conversation_transition_shutdown_requested = False
        connection.is_running = False
        assert not native_history.connection_reader(app)
    finally:
        owner.cancel()
        with suppress(asyncio.CancelledError):
            await owner
        app._conversation_transition_task = None
        app._conversation_transition_pending = False
        app._native_history_open_capture = None
        app._native_history_open_workers = []
        app._native_history_open_roles = None


async def test_captured_hydration_failure_is_preserved_for_open_settlement(switch_app, monkeypatch):
    import inspect
    from types import SimpleNamespace

    from litetui import model_transport
    from litetui.app import LiteTUI

    app = switch_app
    common(app)

    async def failed():
        raise model_transport.ProviderError("controlled hydration failure")

    monkeypatch.setattr(model_transport, "for_app", lambda _app: SimpleNamespace(refresh_history=failed))
    monkeypatch.setattr(native_history, "connection_reader", lambda _app: True)
    await inspect.unwrap(LiteTUI._refresh_native_history)(app, app.convo_path)
    assert app._native_history_open_history_error == "Provider history refresh failed: ProviderError"
    app._native_history_open_history_error = None
    monkeypatch.setattr(native_history, "connection_reader", lambda _app: False)
    await inspect.unwrap(LiteTUI._refresh_native_history)(app, app.convo_path)
    assert app._native_history_open_history_error is None


@pytest.mark.parametrize("finished", [True, False])
async def test_cleanup_joins_only_capture_workers_and_retains_unproven_settlement(finished):
    from types import SimpleNamespace

    from litetui import conversation_shutdown

    calls = []

    async def owned_wait():
        calls.append("owned")
        raise RuntimeError("controlled worker failure")

    async def foreign_wait():
        pytest.fail("Foreign replacement worker must not be joined")

    app = SimpleNamespace(
        _native_history_open_workers=[SimpleNamespace(wait=owned_wait, is_finished=finished)],
        _resume_connect_worker=SimpleNamespace(wait=foreign_wait),
        _native_history_worker=SimpleNamespace(wait=foreign_wait),
        _conversation_transition_protocol=True,
        _conversation_transition_pending=False,
        convo_id="current",
    )
    result = {"status": "connected", "close_required": False, "error": None}
    await native_history._settle_workers(app, result)
    assert calls == ["owned"]
    assert result["status"] == "failed" and result["close_required"]
    assert result["worker_settled"] is finished
    if finished:
        assert not getattr(app, "_conversation_transition_cleanup_error", None)
        assert (await conversation_shutdown.settle_before_shutdown(app))["worker_settled"]
    else:
        with pytest.raises(RuntimeError, match="unproven process cleanup"):
            await conversation_shutdown.settle_before_shutdown(app)


@pytest.mark.parametrize("mode", ["legacy", "startup", "captured"])
async def test_negotiated_connect_skips_automatic_thinking_inference_only(switch_app, monkeypatch, mode):
    from inspect import unwrap

    from litetui import launch_options
    from litetui.app import LiteTUI

    app = switch_app
    probes = []

    async def prepared(_app):
        return "ok"

    monkeypatch.setattr(launch_options, "prepare", prepared)
    monkeypatch.setattr(type(app.backend), "name", "lmstudio")
    app._cli_thinking_level = None
    app._probe_thinking = lambda: probes.append("thinking inference")
    if mode != "legacy":
        common(app)
    # This test isolates the negotiated/legacy branch in the actual connect body;
    # process fixtures exercise registration with real captured workers.
    app._native_history_open_capture = object() if mode == "captured" else None
    await unwrap(LiteTUI.connect)(app)
    assert app._gui_connection_success, getattr(app, "_resume_connection_error", None)
    assert probes == (["thinking inference"] if mode == "legacy" else [])


@pytest.mark.parametrize("ambient_role", ["_resume_connect_worker", "_native_history_worker"])
async def test_tracked_context_worker_cannot_be_promoted_to_execution_reader(switch_app, monkeypatch, ambient_role):
    import asyncio
    from contextlib import suppress
    from types import SimpleNamespace

    from litetui import conversation_transition

    app = switch_app
    common(app)
    owner = asyncio.create_task(asyncio.Event().wait())
    connection = SimpleNamespace(is_running=True)
    context = SimpleNamespace(is_running=True)
    history = SimpleNamespace(is_running=True)
    current = [connection]
    monkeypatch.setattr("textual.worker.get_current_worker", lambda: current[0])
    app._conversation_transition_task = owner
    app._conversation_transition_pending = True
    app._native_history_open_capture = native_history.capture_file(app.convo_path)
    app._resume_connect_worker = connection
    app._native_history_worker = history
    app._native_history_open_workers = [connection]
    app._native_history_open_roles = native_history._WorkerRoles(
        app._native_history_open_capture, owner, connection)
    try:
        assert native_history.track_connection_worker(app, context)
        # Shutdown blocks reads, but must still bind a late canonical hydration.
        app._conversation_transition_shutdown_requested = True
        native_history.track_history_worker(app, history)
        assert app._native_history_open_roles.history is history
        app._conversation_transition_shutdown_requested = False
        original_roles = app._native_history_open_roles
        setattr(app, ambient_role, context)
        current[0] = context
        assert not native_history.connection_reader(app)
        with pytest.raises(ValueError, match="transition"):
            conversation_transition.require_open(app)
        with pytest.raises(ValueError, match="transition"):
            conversation_transition.require_execution(app)
        with pytest.raises(ValueError, match="captured connection"):
            native_history.track_connection_worker(app, SimpleNamespace(is_running=True))
        assert app._native_history_open_roles is original_roles
        assert any(worker is context for worker in app._native_history_open_workers)
    finally:
        owner.cancel()
        with suppress(asyncio.CancelledError):
            await owner
        app._conversation_transition_task = None
        app._conversation_transition_pending = False
        app._native_history_open_capture = None
        app._native_history_open_workers = []
        app._native_history_open_roles = None


async def test_image_preview_requires_materialized_owner_exact_block_and_embedded_bytes(switch_app, tmp_path):
    import io

    from PIL import Image

    app = switch_app
    def url(color):
        with io.BytesIO() as out:
            Image.new("RGBA", (120, 60), color).save(out, format="PNG")
            return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")
    path = tmp_path / "external.png"
    path.write_bytes(base64.b64decode(url("red").partition(",")[2]))
    cases = [url("#ff0000"), url("#00ff00"), "https://example.invalid/remote.png", path.as_uri(),
             str(path), "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png;base64,malformed!"]
    message = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": u}} for u in cases]}
    save_history(app, [message])
    command = common(app)
    before = disk(app)
    snapshot = native_history.snapshot(app, command)
    rows = read_page(app, command, snapshot)["fragments"]
    def select(row):
        return {**command, "id": "preview", "type": "gui.history.native_image", "snapshot": snapshot["snapshot"],
                **{k: row[k] for k in ("block_id", "block_index", "message_index", "message_sha256")}}
    red, green = [native_history.image(app, select(r)) for r in rows[:2]]
    assert (red["width"], red["height"]) == (64, 32)
    assert red["message_sha256"] == green["message_sha256"] and red["block_id"] != green["block_id"]
    assert base64.b64decode(red["rgba"]) == bytes([255, 0, 0, 255]) * (64 * 32)
    assert base64.b64decode(green["rgba"]) == bytes([0, 255, 0, 255]) * (64 * 32)
    for row in rows[2:]:
        with pytest.raises(ValueError, match="image"):
            native_history.image(app, select(row))
    for overrides in ({"block_id": rows[1]["block_id"]}, {"message_sha256": "0" * 64}, {"agent_id": "foreign"},
                      {"session_id": str(uuid.uuid4())}, {"path": str(path)}, {"url": "file:///never"}):
        with pytest.raises(ValueError):
            native_history.image(app, {**select(rows[0]), **overrides})
    assert disk(app) == before
    target = str(uuid.uuid4())
    directory = app._agent_session.conversation_directory(target)
    directory.mkdir()
    records = [{"type": "meta", "id": target}, {"type": "msg", "message": message}]
    (directory / "convo.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf8")
    sibling = common(app, scope="preview", session_id=target, catalog_snapshot=conversation_page._catalog(app, app.convo_id)[4])
    saved = native_history.snapshot(app, sibling)
    block = read_page(app, sibling, saved)["fragments"][1]
    with pytest.raises(ValueError, match="changed"):
        native_history.image(app, {**select(block), **sibling})
    other = native_history.image(app, {**select(block), **sibling, "snapshot": saved["snapshot"]})
    assert other["rgba"] == green["rgba"] and other["session_id"] == target and other["from_session_id"] == app.convo_id
    save_history(app, [{"role": "user", "content": "replaced"}])
    with pytest.raises(ValueError, match="changed"):
        native_history.image(app, select(rows[0]))


@pytest.mark.parametrize("target_mode", ["home", "owner"])
async def test_open_refuses_a_foreign_store_before_leaving_current_conversation(switch_app, tmp_path, monkeypatch, target_mode):
    from litetui import background_input, hook_host
    from litetui.conversation import ConversationRepository

    app = switch_app
    authority = app._agent_session.authority
    app._owner_memory_dir = tmp_path / "current-owner"
    app._owner_memory_dir.mkdir()
    saved = ConversationRepository(agent_session=app._agent_session)
    saved.stage(str(uuid.uuid4()))
    saved.acquire()
    try:
        saved.pending = False
        saved.record_meta(app.model_id, authority.name, authority.agent_id)
        saved.record_snapshot([{"role": "system", "content": "Other store"}, {"role": "user", "content": "saved"}],
                              store_identity={"version": 1, "mode": target_mode, "folder": str(tmp_path / "foreign")},
                              store_injected=True)
        assert not saved.persist_error
    finally:
        saved.release()
    catalog = conversation_page._catalog(app, app.convo_id)[4]
    cmd = common(app, session_id=saved.convo_id, scope="preview", catalog_snapshot=catalog)
    target = native_history.snapshot(app, cmd)
    before = disk(app), app.convo_id, app.store, app.conversation
    def unauthorized(*args, **kwargs):
        pytest.fail("Foreign store proceeded into an identity transition")
    monkeypatch.setattr(background_input, "before_identity_transition", unauthorized)
    monkeypatch.setattr(hook_host, "leave_conversation", unauthorized)
    result = await native_history.open_conversation(app, {**cmd, "history_snapshot": target["snapshot"],
                                                        "action_id": target["open_action_id"]})
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"]
    assert "different memory stores" in result["error"]
    assert before == (disk(app), app.convo_id, app.store, app.conversation)
