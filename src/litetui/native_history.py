"""Owned local history inspection and compact guarded opening (protocol v1).

Reads are pure: canonical JSONL replay runs over one captured byte string. No
provider hydration, card summarization, materialization or execution is permitted.
Existing UI/CLI integration explicitly opts in before startup replay.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import io
import json
import secrets
import stat
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, cast

from litetui import backend_catalog, conversation_page
from litetui import history_projection as projection
from litetui.agent_store import _unlinked, valid_id
from litetui.conversation import ConversationRepository
from litetui.history_index import HistoryIndex

VERSION = 1
MAX_TRANSCRIPT = 8 * 1024 * 1024
MAX_RESPONSE = 1024 * 1024
MAX_CURSOR = 2048
COMMON = ("from_session_id", "session_id", "agent_id", "backend", "model", "scope", "catalog_snapshot")


@dataclass(frozen=True)
class Captured:
    path: Path | None
    raw: bytes | None
    meta_bytes: bytes
    messages_bytes: bytes
    summaries_bytes: bytes
    writable: bool = True

    @property
    def meta(self) -> dict[str, Any]:
        return json.loads(self.meta_bytes)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return json.loads(self.messages_bytes)

    @property
    def summaries(self) -> dict[str, str]:
        return json.loads(self.summaries_bytes)

    @property
    def revision(self) -> str | None:
        return projection.digest(self.raw) if self.raw is not None else None


@dataclass(frozen=True)
class _ProjectionCache:
    owner: Any
    key: tuple[str, str | None, str, str]
    index: HistoryIndex


def _index(app: Any, capture: Captured, session_id: str) -> HistoryIndex:
    # Capture and live/authority validation still run on every request. Reuse
    # only immutable presentation for this exact semantic and raw revision.
    key = (session_id, capture.revision, projection.digest(capture.messages_bytes),
           projection.digest(capture.summaries_bytes))
    cached = getattr(app, "_native_history_projection_cache", None)
    if isinstance(cached, _ProjectionCache) and cached.owner is app._agent_session and cached.key == key:
        return cached.index
    app._native_history_projection_cache = None
    cached = None
    index = HistoryIndex.build(projection.project(capture.messages, capture.summaries))
    app._native_history_projection_cache = _ProjectionCache(app._agent_session, key, index)
    return index


class _BytesPath:
    """Open-only input adapter for the repository's single canonical replay.

    Both existing readers only use path.open('rb'); keeping them on the same
    immutable bytes avoids a second replay implementation or a read-again race.
    """
    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, mode: str) -> io.BytesIO:
        if mode != "rb":
            raise ValueError("Captured history is read-only")
        return io.BytesIO(self.data)


def capture_bytes(data: bytes, path: Path | None = None) -> Captured:
    if not isinstance(data, bytes) or len(data) > MAX_TRANSCRIPT:
        raise ValueError("History transcript exceeds the 8 MiB file budget")
    source = cast(Path, _BytesPath(data))
    meta, messages = ConversationRepository.read(source, max_bytes=MAX_TRANSCRIPT)
    summaries = ConversationRepository.card_summaries(source)
    return Captured(path, data, projection.encoded(meta), projection.encoded(messages), projection.encoded(summaries))


def _stamp(info: Any) -> tuple[int, ...]:
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_mode & 0o222)


def capture_file(path: Path) -> Captured:
    import os

    path = _unlinked(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_TRANSCRIPT:
        raise ValueError("History transcript is not a bounded regular file")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(MAX_TRANSCRIPT + 1)
        finished = os.fstat(stream.fileno())
    after = _unlinked(path).stat()
    if len(data) > MAX_TRANSCRIPT or not (_stamp(before) == _stamp(opened) == _stamp(finished) == _stamp(after)):
        raise ValueError("History transcript changed while reading")
    captured = capture_bytes(data, path)
    return Captured(captured.path, captured.raw, captured.meta_bytes, captured.messages_bytes,
                    captured.summaries_bytes, bool(after.st_mode & 0o222))


def enabled(app: Any) -> bool:
    value = getattr(getattr(app, "_launch_options", None), "history_projection_version", None)
    return type(value) is int and value == VERSION


def _lifetime(app: Any) -> bytes:
    owner = getattr(app, "_native_history_owner", None)
    if owner is not app._agent_session:
        app._native_history_owner = app._agent_session
        app._native_history_secret = secrets.token_bytes(32)
    return app._native_history_secret


def _wire(cmd: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {"type": "response", "ok": True}
    if cmd.get("id") is not None:
        response["id"] = cmd["id"]
    response["result"] = result
    if len(json.dumps(response, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("History response exceeds the full 1 MiB wire budget")
    return result


def _token(value: Any) -> str:
    import re

    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Invalid history snapshot token")
    return value


def _common(app: Any, cmd: dict[str, Any]) -> Any:
    if not enabled(app):
        raise ValueError("Restart with history projection version 1 before using native history")
    if getattr(app, "_rpc", False) is not True or getattr(app, "_hold_restored_input", False) is not True:
        raise ValueError("Native history requires RPC and restored-input holds")
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Native history requires version 1")
    if len(json.dumps(cmd.get("id"), allow_nan=False).encode("utf-8")) > 4096:
        raise ValueError("History request id exceeds its metadata budget")
    context = backend_catalog._context(app, cmd.get("from_session_id"))
    expected = {"agent_id": context.authority.agent_id, "backend": context.selected,
                "model": app.model_id, "from_session_id": app.convo_id}
    if any(cmd.get(key) != value for key, value in expected.items()):
        raise ValueError("History request belongs to another owned identity")
    valid_id(cmd.get("session_id"))
    if cmd.get("scope") == "current":
        if cmd["session_id"] != app.convo_id or cmd.get("catalog_snapshot") is not None:
            raise ValueError("Current history must name the exact active owned conversation")
    elif cmd.get("scope") == "preview":
        _token(cmd.get("catalog_snapshot"))
    else:
        raise ValueError("History scope must be current or preview")
    return context


def _preview(app: Any, cmd: dict[str, Any]) -> tuple[Any, Any]:
    before = conversation_page._catalog(app, cmd["from_session_id"])
    session, _store, authority, records, token = before
    if cmd["catalog_snapshot"] != token or authority.agent_id != cmd["agent_id"]:
        raise ValueError("Conversation catalog changed; refresh before previewing")
    record = next((row for row in records if row.session_id == cmd["session_id"]), None)
    if record is None:
        raise ValueError("Preview target is outside the owned conversation catalog")
    if record.path != session.conversation_directory(record.session_id) / "convo.jsonl":
        raise ValueError("Preview target path differs from its owned identity")
    return record, before


def _collect(app: Any, cmd: dict[str, Any]) -> tuple[dict[str, Any], Captured, HistoryIndex]:
    context = _common(app, cmd)
    objects = (app.conversation, app.backend, app.settings, app.store, app._agent_session)
    before = backend_catalog.fingerprint(app, context, {"kind": "history", "version": VERSION})
    catalog = None
    pending = cmd["scope"] == "current" and bool(app.store.pending)
    if pending:
        if app.store.convo_path.exists():
            raise ValueError("Pending conversation unexpectedly has a durable transcript")
        capture = Captured(None, None, projection.encoded({"id": app.convo_id}),
                           projection.encoded(app.conversation), b"{}")
    else:
        if cmd["scope"] == "preview":
            record, catalog = _preview(app, cmd)
            path = record.path
        else:
            path = app.store.convo_path
        capture = capture_file(path)
        if capture.meta.get("id") != cmd["session_id"]:
            raise ValueError("History transcript identity disagrees with its owned directory")
        if cmd["scope"] == "current" and capture.messages_bytes != projection.encoded(app.conversation):
            raise ValueError("Saved history differs from the current live context; wait for settlement")
    index = _index(app, capture, cmd["session_id"])
    view = index.view
    title = projection.text(ConversationRepository.label(capture.meta, capture.messages), 4096)
    result = {"version": VERSION, "kind": "history_snapshot", "projection_version": projection.VERSION,
              **{key: cmd[key] for key in COMMON}, "pending": pending, "materialized": not pending,
              "title": title, "read_only": not capture.writable, "transcript_revision": capture.revision,
              "context_sha256": view.context_sha256, "replay_sha256": view.replay_sha256,
              "fresh_system_context": view.fresh_system_context, "counts": view.counts()}
    from litetui import conversation_transition as transition
    from litetui import hook_host
    hooks, registry = transition._hooks(app), transition._registry(app)
    if hooks.error:
        raise ValueError(hooks.error)
    source = hook_host.context(app)["source"]
    effects = None
    if cmd["scope"] == "preview":
        events = ["conversation_resume"] + (["conversation_leave"] if getattr(app, "_hook_conversation_id", None) else [])
        effects = {"materialize": False, "provider_cleanup": bool(getattr(app.backend, "owns_native_turns", False)),
                   "lifecycle_hooks": any(transition._matching(hooks, event, source) for event in events)}
    result["open_effects"] = effects
    facts = {"descriptor": result, "runtime": before, "objects": [id(value) for value in objects],
             "hooks": asdict(hooks), "registry": registry, "hook_context": hook_host.context(app)}
    action = hmac.digest(_lifetime(app), b"open:" + projection.encoded(facts), "sha256")[:16]
    result["open_action_id"] = str(uuid.UUID(bytes=action, version=4)) if effects is not None else None
    result["snapshot"] = hmac.digest(_lifetime(app), projection.encoded(facts), "sha256").hex()
    if any(old is not new for old, new in zip(objects, (app.conversation, app.backend, app.settings, app.store, app._agent_session))):
        raise ValueError("History ownership changed during inspection")
    if backend_catalog.fingerprint(app, _common(app, cmd), {"kind": "history", "version": VERSION}) != before:
        raise ValueError("History runtime changed during inspection")
    if transition._hooks(app) != hooks or transition._registry(app) != registry or hook_host.context(app)["source"] != source:
        raise ValueError("History lifecycle configuration changed during inspection")
    if catalog is not None:
        conversation_page._unchanged(app, cmd["from_session_id"], catalog)
    if capture.path is not None and capture_file(capture.path).revision != capture.revision:
        raise ValueError("History changed during projection; refresh before continuing")
    return result, capture, index


def snapshot(app: Any, cmd: dict[str, Any]) -> dict[str, Any]:
    result = _wire(cmd, _collect(app, cmd)[0])
    app._conversation_transition_protocol = True
    return result


def _cursor(snapshot_token: str, position: tuple[int, int], secret: bytes) -> str:
    data = projection.encoded({"version": VERSION, "snapshot": snapshot_token,
                               "block": position[0], "utf8_offset": position[1]}, 1024)
    payload = base64.urlsafe_b64encode(data).decode("ascii")
    return payload + "." + hmac.digest(secret, data, "sha256").hex()


def _position(cursor: Any, snapshot_token: str, secret: bytes) -> tuple[int, int]:
    projection.text(cursor, MAX_CURSOR, empty=False)
    try:
        payload, signature = cursor.split(".")
        raw = base64.b64decode(payload, altchars=b"-_", validate=True)
        if not hmac.compare_digest(signature, hmac.digest(secret, raw, "sha256").hex()):
            raise ValueError("History cursor was not issued by this owned runtime")
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid history page cursor") from exc
    if not isinstance(value, dict):
        raise TypeError("Invalid history page cursor")
    block, offset = value.get("block"), value.get("utf8_offset")
    if (type(block) is not int or type(offset) is not int or not 0 <= block <= projection.MAX_COUNT
            or not 0 <= offset <= projection.MAX_PROJECTION or cursor != _cursor(snapshot_token, (block, offset), secret)):
        raise ValueError("History cursor is stale or belongs to another view")
    return block, offset


def page(app: Any, cmd: dict[str, Any]) -> dict[str, Any]:
    expected = _token(cmd.get("snapshot"))
    descriptor, _capture, index = _collect(app, cmd)
    view = index.view
    if descriptor["snapshot"] != expected:
        raise ValueError("History changed; refresh the saved-history view")
    cursor = cmd.get("cursor")
    if cursor is None:
        if cmd.get("anchor") not in ("head", "tail"):
            raise ValueError("Initial history page requires head or tail anchor")
        wanted = None
    else:
        if "anchor" in cmd:
            raise ValueError("A history cursor must not be combined with an anchor")
        wanted = _position(cursor, expected, _lifetime(app))
    start, end, fragments, previous = index.window(wanted, anchor=cmd.get("anchor", "head"))
    terminal = (len(view.blocks), 0)
    result = {"version": VERSION, "kind": "history_page", "projection_version": projection.VERSION,
              **{key: descriptor[key] for key in COMMON}, "snapshot": expected,
              "context_sha256": view.context_sha256, "replay_sha256": view.replay_sha256,
              "counts": view.counts(), "fragments": fragments, "at_start": start == (0, 0),
              "at_end": end == terminal, "previous_cursor": _cursor(expected, previous, _lifetime(app)) if previous is not None else None,
              "next_cursor": _cursor(expected, end, _lifetime(app)) if end != terminal else None}
    return _wire(cmd, result)


def image(app: Any, cmd: dict[str, Any]) -> dict[str, Any]:
    """Preview only the embedded bytes of the identified saved display block."""
    from PIL import Image, UnidentifiedImageError

    from litetui import native_image
    from litetui.conversation_export import image_url

    keys = {"type", "id", "version", *COMMON, "snapshot", "block_index", "block_id", "message_index", "message_sha256"}
    if set(cmd) != keys or cmd["type"] != "gui.history.native_image":
        raise ValueError("Unexpected fields in saved image request.")
    expected = _token(cmd["snapshot"])
    descriptor, capture, index = _collect(app, cmd)
    if descriptor["snapshot"] != expected or not descriptor["materialized"]:
        raise ValueError("Saved image snapshot changed. Refresh history.")
    number = cmd["block_index"]
    if type(number) is not int or not 0 <= number < len(index.view.blocks) or type(cmd["message_index"]) is not int:
        raise ValueError("Invalid saved image selection.")
    block = index.view.blocks[number]
    if (block.kind != "attachment" or block.content_index is None or block.block_id != cmd["block_id"]
            or block.message_index != cmd["message_index"] or block.message_sha256 != cmd["message_sha256"]):
        raise ValueError("Image selection does not match the saved message and block.")
    content = capture.messages[block.message_index]["content"][block.content_index]
    if not isinstance(content, dict) or content.get("type") not in ("image_url", "input_image"):
        raise ValueError("The saved content is not an image attachment.")
    url = image_url(content)
    formats = {"data:image/png;base64": "PNG", "data:image/jpeg;base64": "JPEG", "data:image/webp;base64": "WEBP"}
    if not isinstance(url, str) or not url.startswith("data:image/"):
        raise ValueError("Only saved embedded image data can be previewed.")
    prefix, separator, encoded = url.partition(",")
    if prefix not in formats or not separator or not 0 < len(encoded) <= 4 * ((native_image.MAX_FILE + 2) // 3):
        raise ValueError("Unsupported or oversized saved image.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > native_image.MAX_FILE:
            raise ValueError
        with Image.open(io.BytesIO(raw), formats=[formats[prefix]]) as source:
            if source.width * source.height > native_image.MAX_PIXELS or getattr(source, "is_animated", False):
                raise ValueError
            pixels = native_image.thumbnail(source)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("Cannot preview this saved image as a bounded still PNG, JPEG or WebP.") from exc
    # The decoder can release the GIL. Recheck the same owned proof after work.
    if _collect(app, cmd)[0]["snapshot"] != expected:
        raise ValueError("Saved image changed during inspection.")
    return _wire(cmd, {"kind": "history_image", "version": VERSION, **{k: descriptor[k] for k in COMMON},
                       **{k: cmd[k] for k in ("snapshot", "block_index", "block_id", "message_index", "message_sha256")},
                       **pixels})


def _current_command(app: Any) -> dict[str, Any]:
    return {"version": VERSION, "from_session_id": app.convo_id, "session_id": app.convo_id,
            "agent_id": app._agent_session.authority.agent_id, "backend": app.backend.name,
            "model": app.model_id, "scope": "current", "catalog_snapshot": None}



@dataclass(frozen=True)
class _WorkerRoles:
    capture: Captured
    owner: asyncio.Task[Any]
    connection: Any
    history: Any = None


def _capture_worker(app: Any) -> Any:
    """Return an exact running capture worker, including during shutdown."""
    from textual.worker import NoActiveWorker, get_current_worker

    if (not enabled(app) or not getattr(app, "_conversation_transition_pending", False)
            or not isinstance(getattr(app, "_native_history_open_capture", None), Captured)):
        return None
    roles = getattr(app, "_native_history_open_roles", None)
    if (not isinstance(roles, _WorkerRoles)
            or roles.capture is not app._native_history_open_capture
            or roles.owner is not getattr(app, "_conversation_transition_task", None)
            or roles.owner.done()):
        return None
    try:
        worker = get_current_worker()
    except NoActiveWorker:
        return None
    if not worker.is_running:
        return None
    tracked = getattr(app, "_native_history_open_workers", ())
    if not any(roles.connection is item for item in tracked):
        return None
    if (any(worker is item for item in tracked)
            and (worker is roles.connection and worker is getattr(app, "_resume_connect_worker", None)
                 or worker is roles.history and worker is getattr(app, "_native_history_worker", None))):
        return worker
    return None


def connection_reader(app: Any) -> bool:
    """Read-only access for this captured open's exact running Textual worker."""
    return (not getattr(app, "_conversation_transition_shutdown_requested", False)
            and _capture_worker(app) is not None)


def track_connection_worker(app: Any, worker: Any) -> bool:
    """Bind a spawned descendant before its first await, even while closing."""
    if not isinstance(getattr(app, "_native_history_open_capture", None), Captured):
        return False
    roles = getattr(app, "_native_history_open_roles", None)
    if not isinstance(roles, _WorkerRoles) or _capture_worker(app) is not roles.connection:
        raise ValueError("Native worker was not created by the captured connection")
    app._native_history_open_workers.append(worker)
    return True


def track_history_worker(app: Any, worker: Any) -> None:
    """Bind hydration at its canonical spawn seam, before its first await."""
    if not isinstance(getattr(app, "_native_history_open_capture", None), Captured):
        return
    if worker is not getattr(app, "_native_history_worker", None):
        raise ValueError("Native history worker does not match the captured connection")
    track_connection_worker(app, worker)
    roles = app._native_history_open_roles
    if roles.history is not None and roles.history is not worker:
        raise ValueError("Captured hydration worker role is already bound")
    app._native_history_open_roles = replace(roles, history=worker)


def guarded_capture(app: Any, capture: Any, path: Path) -> bool:
    """Only the exact internal transition capture can bypass its own resume gate."""
    if capture is None:
        return False
    valid = (isinstance(capture, Captured) and enabled(app) and capture.path == path
             and getattr(app, "_native_history_open_capture", None) is capture
             and getattr(app, "_conversation_transition_pending", False)
             and getattr(app, "_conversation_transition_task", None) is asyncio.current_task())
    if not valid:
        raise ValueError("Captured resume does not belong to the active owned transition")
    return True


async def resume_capture(app: Any, capture: Captured) -> None:
    """Canonical resume adoption with recorded/joined leave and resume hooks."""
    from litetui import conversation_transition as transition
    from litetui import hook_host
    from litetui.claude_backend import close_native
    from litetui.gui_rpc import _tool_policy

    if capture.path is None or not guarded_capture(app, capture, capture.path):
        raise ValueError("No owned captured resume is active")
    review = app._native_history_open_review
    target = capture.path.parent.name
    policy = _tool_policy(app)
    authority = app._agent_session.authority
    if getattr(app.backend, "owns_native_turns", False):
        app._conversation_transition_cleanup_error = "Outgoing native cleanup has not been verified"
        bridge = getattr(app.backend, "_claude_tools", None)
        if bridge:
            bridge.stop()
        close_native(app, app.backend)
        await transition._joined(transition._close_native(app))
    transition._unchanged(app, review)
    if getattr(app, "_hook_conversation_id", None):
        hook_host.leave_conversation(app)
        await transition._settle_hooks(app, review, "conversation_leave", review.context.session_id, 0)
        transition._unchanged(app, review)
    if backend_catalog._disk_hash(app.store.convo_path, MAX_TRANSCRIPT) != review.transcript:
        raise ValueError("Outgoing transcript changed before opening another conversation")
    if capture_file(capture.path).revision != capture.revision:
        raise ValueError("Target transcript changed after the open review")
    # The catalog also binds destination settings. A hook must not replace them
    # between review and adoption, even if it leaves transcript bytes unchanged.
    target_settings = capture.path.parent / "settings.json"
    if backend_catalog._disk_hash(target_settings, conversation_page.MAX_SETTINGS) != app._native_history_target_settings:
        raise ValueError("Target settings changed after the open review")
    from litetui import convo_settings, native_runtime_controls, settings_runtime, tool_policy
    if native_runtime_controls.enabled(app) and policy["tool_profile_requested"] is None:
        saved = settings_runtime.service_for(app).snapshot(target, read_only=True,
                                                            max_bytes=conversation_page.MAX_SETTINGS)
        selected = convo_settings.resolved(convo_settings.load(capture.path.parent), saved.saved, "tool_policy_profile")
        if selected not in tool_policy.PROFILES:
            raise ValueError("Saved target has an unknown tool profile")
        policy["tool_profile"] = selected
    offset = len(app._conversation_transition_hook_outcomes)
    if not app._resume(capture.path, _native_capture=capture):
        raise ValueError("Canonical captured conversation resume was refused")
    await transition._settle_hooks(app, review, "conversation_resume", target, offset)

    def unchanged() -> None:
        if (app.convo_id != target or app.store.convo_id != target or app._agent_session is not review.context.session
                or app.backend is not review.context.backend or app._agent_session.authority != authority
                or _tool_policy(app) != policy or transition._hooks(app) != review.hooks
                or transition._registry(app) != review.registry
                or getattr(app, "_conversation_transition_shutdown_requested", False)):
            raise ValueError("Opened conversation changed owned identity, policy or lifecycle configuration")

    unchanged()
    if getattr(app, "_resume_backend_error", None):
        raise ValueError(app._resume_backend_error)
    connection = app.connect()
    app._resume_connect_worker = connection
    app._native_history_open_workers.append(connection)
    app._native_history_open_roles = _WorkerRoles(capture, cast(asyncio.Task[Any], asyncio.current_task()), connection)
    await transition._joined(connection.wait())
    unchanged()
    if (getattr(app, "_resume_connection_error", None) or not getattr(app, "_connect_settled", False)
            or not getattr(app, "_gui_connection_success", False)):
        raise ValueError(getattr(app, "_resume_connection_error", None) or "Conversation connection did not complete")
    roles = app._native_history_open_roles
    worker = roles.history
    if getattr(app, "_native_history_worker", None) is not worker:
        raise ValueError("Native history worker changed its captured role during opening")
    if worker is not None:
        if not any(worker is item for item in app._native_history_open_workers):
            raise ValueError("Native history worker is not owned by this captured opening")
        await transition._joined(worker.wait())
        unchanged()
        if app._native_history_worker is not worker:
            raise ValueError("Native history worker changed during opening")
        if getattr(app, "_native_history_open_history_error", None):
            raise ValueError(app._native_history_open_history_error)
    if app._resume_connect_worker is not connection:
        raise ValueError("Conversation connection worker changed during opening")
    backend_catalog._context(app, target)


async def open_conversation(app: Any, cmd: dict[str, Any]) -> dict[str, Any]:
    """Consume reviewed local history, retaining the lifecycle owner until joined."""
    from litetui import backend_transition, background_input, hook_host, native_engine
    from litetui import conversation_transition as transition

    result: dict[str, Any] = {"version": VERSION, "kind": "conversation_open",
        "from_session_id": cmd.get("from_session_id"), "target_session_id": cmd.get("session_id"),
        "catalog_snapshot": cmd.get("catalog_snapshot"), "history_snapshot": cmd.get("history_snapshot"),
        "action_id": cmd.get("action_id"), "session_id": app.convo_id, "agent_id": cmd.get("agent_id"),
        "backend": app.backend.name, "model": app.model_id, "status": "refused", "mutated": False,
        "close_required": False, "error": None, "worker_settled": False, "connection_settled": False,
        "history_settled": False, "history": None}
    try:
        before, capture, _view = _collect(app, {**cmd, "scope": "preview"})
        if (_token(cmd.get("history_snapshot")) != before["snapshot"]
                or cmd.get("action_id") != before["open_action_id"]
                or cmd.get("action_id") == getattr(app, "_native_history_consumed", None)):
            raise ValueError("Reviewed history changed or was consumed; refresh before opening")
        if before["session_id"] == app.convo_id:
            raise ValueError("Conversation is already open")
        record, _catalog = _preview(app, {**cmd, "scope": "preview"})
        if before["read_only"]:
            raise ValueError("Conversation transcript is read-only")
        reason = conversation_page._compatibility(app, app._agent_session.authority, record)
        if reason:
            raise ValueError(reason)
        if not capture.messages:
            raise ValueError("Conversation has no saved context to resume")
        # Resolve store provenance before leaving the current conversation or running its hooks.
        from litetui.store_identity import resume_store_match
        resume_store_match(app, record.path, capture.meta, capture.messages)
        backend_transition._quiet(app, require_unowned_server=False)
        if native_engine.state(app) is not None:
            raise ValueError("Reopen explicitly before changing a native-engine conversation")
        if reason := transition._eligibility(app):
            raise ValueError(reason)
        context = backend_catalog._context(app, app.convo_id)
        review = transition.Review(projection.encoded(before), context, transition._hooks(app), transition._registry(app),
            transition._protected(app), projection.encoded(app.conversation),
            backend_catalog._disk_hash(app.store.convo_path, MAX_TRANSCRIPT),
            backend_catalog._disk_hash(app.store.convo_dir / "settings.json", 1024 * 1024), hook_host.context(app)["source"])
        transition._unchanged(app, review)
        target_settings = backend_catalog._disk_hash(record.path.parent / "settings.json", conversation_page.MAX_SETTINGS)
    except Exception as exc:  # noqa: BLE001 - no effects have begun
        result["error"] = _error(exc)
        return _wire(cmd, result)
    app._native_history_consumed = cmd["action_id"]
    app._native_history_open_capture = capture
    app._native_history_open_review = review
    app._native_history_target_settings = target_settings
    app._native_history_open_workers = []
    app._native_history_open_roles = None
    app._native_history_open_history_error = None
    app._conversation_transition_protocol = True
    app._conversation_transition_shutdown_identity = {
        "action_id": cmd["action_id"], "action": "open", "from_session_id": cmd["from_session_id"],
        "target_session_id": cmd["session_id"], **{key: cmd[key] for key in ("agent_id", "backend", "model")}}
    app._conversation_transition_task = asyncio.current_task()
    app._conversation_transition_pending = True
    app._conversation_transition_hook_outcomes = []
    app._conversation_transition_hook_snapshot = review.hooks
    work_proof = None
    try:
        work_proof = await background_input.before_identity_transition(app)
        await resume_capture(app, capture)
        result.update(worker_settled=True, connection_settled=True)
        result["history"] = snapshot(app, _current_command(app))
        result.update(status="connected", mutated=True, history_settled=True)
    except BaseException as exc:  # noqa: BLE001 - all post-effect failures close the source admission gate
        app._conversation_transition_error = transition.CLOSE_REQUIRED
        result.update(status="failed", mutated=app.convo_id != cmd["from_session_id"], close_required=True, error=_error(exc))
    finally:
        await _settle_workers(app, result)
        app._conversation_transition_pending = False
        app._conversation_transition_task = None
        app._conversation_transition_hook_outcomes = None
        app._conversation_transition_hook_snapshot = None
        app._native_history_open_capture = None
        app._native_history_open_review = None
        app._native_history_open_workers = []
        app._native_history_open_roles = None
        app._native_history_open_history_error = None
        app._native_history_target_settings = None
    if result["status"] == "connected":
        try:
            result.update(background_input.after_identity_transition(app, work_proof))
        # Cancellation after a successful open must also close the admission gate.
        except BaseException as exc:  # noqa: BLE001
            app._conversation_transition_error = transition.CLOSE_REQUIRED
            result.update(status="failed", close_required=True, error=_error(exc))
    result.update(session_id=app.convo_id, backend=app.backend.name, model=app.model_id)
    return _wire(cmd, result)



async def _settle_workers(app: Any, result: dict[str, Any]) -> None:
    """Join only workers registered to this capture, retaining failed proof."""
    from litetui import conversation_transition as transition
    from litetui.shutdown_wait import join_task

    settled = True
    for worker in tuple(app._native_history_open_workers):
        try:
            await join_task(asyncio.create_task(worker.wait()))
        except BaseException as exc:  # noqa: BLE001 - worker failure is not a rollback
            app._conversation_transition_error = transition.CLOSE_REQUIRED
            result.update(status="failed", close_required=True, error=result["error"] or _error(exc))
        if getattr(worker, "is_finished", False) is not True:
            settled = False
            app._conversation_transition_cleanup_error = "Captured opening worker settlement is unproven"
            app._conversation_transition_error = transition.CLOSE_REQUIRED
            result.update(status="failed", close_required=True,
                          error=result["error"] or app._conversation_transition_cleanup_error)
    result["worker_settled"] = settled


def _error(error: BaseException) -> str:
    return (str(error) or type(error).__name__).encode("utf-8", "replace")[:4096].decode("utf-8", "ignore")
