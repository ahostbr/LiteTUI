"""Verified native history bounds for a reviewed Codex turn replacement."""
from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass

from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui.backend_session import SessionState
from litetui.codex_compaction_evidence import Binding

MAX_TURNS = 32
MAX_ITEMS = 256
MAX_HISTORY = 1024 * 1024
MAX_REVIEW = 65536


def _digest(value):
    return hashlib.sha256(projection.encoded(value)).hexdigest()


def _id(value):
    if not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", value) is None:
        raise ValueError("Codex history contains an unsupported native identity")
    return value


def text_item(value):
    if (isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict)
            and value[0].get("type") == "text" and not value[0].get("text_elements")):
        return value[0].get("text")
    return None


def row_link(message, *, require_item=True):
    metadata = message.get("provider_metadata")
    if not isinstance(metadata, dict) or metadata.get("provider") != "codex":
        raise ValueError("This saved row lacks a native Codex turn binding")
    item = metadata.get("app_server_user_item_id")
    delivery = message.get("codex_delivery") or {}
    if not isinstance(delivery, dict) or delivery and delivery.get("state") != "accepted":
        raise ValueError("The saved Codex delivery is malformed or unsettled")
    client = delivery.get("id")
    if require_item and not item and not client:
        raise ValueError("This saved row predates native user-item binding. Edit or retry a newly sent message.")
    if client and delivery.get("threadId") != metadata.get("app_server_thread_id"):
        raise ValueError("The saved Codex delivery belongs to another thread")
    return (_id(metadata.get("app_server_thread_id")), _id(metadata.get("app_server_turn_id")),
            _id(item) if item else None, _id(client) if client else None)


async def _history(server, thread):
    async with asyncio.timeout(15):
        meta = (await server.request("thread/read", {"threadId": thread, "includeTurns": False})).get("thread") or {}
        if (meta.get("id") != thread or meta.get("historyMode") != "paginated"
                or (meta.get("status") or {}).get("type") != "idle"):
            raise ValueError("Codex Edit/Retry requires the reviewed idle paginated thread")
        turns = []
        cursor = None
        seen = set()
        while True:
            params = {"threadId": thread, "itemsView": "full", "sortDirection": "asc", "limit": MAX_TURNS}
            if cursor is not None:
                params["cursor"] = cursor
            page = await server.request("thread/turns/list", params)
            data = page.get("data")
            if ("nextCursor" not in page or not isinstance(data, list) or len(turns) + len(data) > MAX_TURNS
                    or len(projection.encoded(data)) > MAX_HISTORY or not data and page.get("nextCursor")):
                raise ValueError("Codex history exceeds the reviewed limit of 32 turns or 1 MiB")
            turns.extend(data)
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("Invalid Codex history continuation")
            seen.add(cursor)
    if len(projection.encoded(turns)) > MAX_HISTORY:
        raise ValueError("Codex history exceeds the 1 MiB review limit")
    turn_ids, item_ids = set(), set()
    for turn in turns:
        turn_id = _id(turn.get("id"))
        items = turn.get("items")
        if (turn_id in turn_ids or turn.get("status") != "completed" or turn.get("itemsView") != "full"
                or not isinstance(items, list) or not items):
            raise ValueError("Codex history contains an incomplete or repeated turn")
        turn_ids.add(turn_id)
        for item in items:
            ident = _id(item.get("id"))
            _id(item.get("type"))
            if ident in item_ids or len(item_ids) >= MAX_ITEMS:
                raise ValueError("Codex history contains repeated items or exceeds the 256 item limit")
            item_ids.add(ident)
    return turns


def _user(turn):
    users = [item for item in turn["items"] if item["type"] == "userMessage"]
    if len(users) != 1 or not isinstance(text_item(users[0].get("content")), str):
        raise ValueError("Codex Edit/Retry requires a single plain user item in every native turn")
    return users[0]


def _match_row(message, thread, turn, *, retained=False):
    # Legacy rows with a native turn but no user item can stay in the retained prefix.
    row_thread, row_turn, row_item, client = row_link(message, require_item=not retained)
    user = _user(turn)
    if (row_thread != thread or row_turn != turn["id"] or row_item and row_item != user["id"]
            or client and client != user.get("clientId") or text_item(user["content"]) != message.get("content")):
        raise ValueError("Saved Codex user row disagrees with native history")
    return user


def _review_item(item):
    kind = item["type"]
    if kind in {"userMessage", "reasoning"}:
        details = ""
    elif kind == "agentMessage":
        details = item.get("text") or ""
    else:
        # Include the actual command, tool, file edits and arguments as recorded.
        details = projection.encoded({k: v for k, v in item.items() if k not in {"id", "type"}}).decode("utf-8")
    return {"id": item["id"], "kind": kind, "details": details}


@dataclass
class Rewind:
    binding: Binding
    before: list
    index: int
    plan: dict
    reverted: bool = False
    claimed: bool = False
    replacement_turn: str | None = None

    @classmethod
    async def review(cls, app, messages, selected):
        binding = Binding.capture(app)
        turns = await _history(binding.server, binding.thread_id)
        binding.check(app)
        rows = [(i, m) for i, m in enumerate(messages) if m.get("role") == "user"]
        if len(rows) != len(turns) or not turns:
            raise ValueError("Saved history does not cover each turn in this native Codex thread")
        first = None
        suffix = []
        for n, ((i, message), turn) in enumerate(zip(rows, turns, strict=True)):
            user = _match_row(message, binding.thread_id, turn, retained=i < selected)
            if i == selected:
                first = n
            if first is not None:
                suffix.append({"turn_id": turn["id"], "user_item_id": user["id"], "text": text_item(user["content"]),
                               "items": [_review_item(item) for item in turn["items"]]})
        if first is None:
            raise ValueError("Selected row lacks a matching Codex user item")
        plan = {"thread_id": binding.thread_id, "before_sha256": _digest(turns),
                "prefix_sha256": _digest(turns[:first]), "retained_turns": first, "suffix": suffix}
        if len(projection.encoded(plan)) > MAX_REVIEW:
            raise ValueError("Codex discarded turns exceed the 64 KiB visible review limit")
        return cls(binding, turns, first, plan)

    async def revert(self, app, attempt):
        self.binding.check(app)
        server, thread = self.binding.server, self.binding.thread_id
        async with self.binding.transport.lock:
            current = await _history(server, thread)
            self.binding.check(app, locked=True)
            action._stable(app, attempt.review, original=True)
            if current != self.before:
                raise ValueError("Native Codex history changed after review")
            if getattr(app, "_stop_requested", False):
                raise ValueError("Stopped before rewinding Codex history")
            attempt.mutated = True
            async with asyncio.timeout(15):
                response = await server.request("thread/revert", {"threadId": thread, "beforeTurnId": self.before[self.index]["id"]})
            meta = response.get("thread") or {}
            if (meta.get("id") != thread or meta.get("historyMode") != "paginated"
                    or (meta.get("status") or {}).get("type") != "idle" or meta.get("turns") != []):
                raise ValueError("Codex rewind returned an unverified thread")
            if await _history(server, thread) != self.before[:self.index]:
                raise ValueError("Codex rewind failed independent retained-prefix readback")
            self.binding.check(app, locked=True)
            self.reverted = True

    def connected(self, app):
        b = self.binding
        activity = b.server.runtime_activity
        if (getattr(app, "_stop_requested", False) or b.transport.turn_id is not None
                or b.session.state != SessionState.READY or b.session.active_turn is not None
                or not activity.connected or activity.uncertain or activity.requests or activity.commands
                or app.backend.app_server is not b.server or app.backend._transport is not b.transport
                or b.transport.thread_id != b.thread_id or b.transport.process is not b.process
                or b.server.process is not b.process or b.process.returncode is not None
                or b.server.session is not b.session or b.session.generation != b.generation
                or b.session.failure or b.session.cleanup_errors or not b.server.initialized):
            raise ValueError("The reviewed Codex thread or lifecycle changed before replacement")

    async def verify_prefix(self, app):
        self.connected(app)
        if await _history(self.binding.server, self.binding.thread_id) != self.before[:self.index]:
            raise ValueError("The retained native prefix changed before the replacement could be sent")
        self.connected(app)

    async def settlement(self, app, messages, selected, text):
        if not self.reverted or not self.claimed or not self.replacement_turn:
            raise ValueError("Codex replacement lacks a verified native execution")
        self.binding.check(app)
        turns = await _history(self.binding.server, self.binding.thread_id)
        self.binding.check(app)
        if (len(turns) != self.index + 1 or turns[:-1] != self.before[:self.index]
                or turns[-1]["id"] != self.replacement_turn or self.replacement_turn in {t["id"] for t in self.before}
                or len(messages) <= selected or messages[selected].get("content") != text):
            raise ValueError("Codex replacement disagrees with the exact reviewed native prefix or message")
        user = _match_row(messages[selected], self.binding.thread_id, turns[-1])
        if user["id"] in {item["id"] for t in self.before for item in t["items"]}:
            raise ValueError("Codex replacement reused an old native item")
        return {"thread_id": self.binding.thread_id, "prefix_sha256": self.plan["prefix_sha256"],
                "turn_id": self.replacement_turn, "user_item_id": user["id"], "history_sha256": _digest(turns)}


@dataclass(frozen=True)
class Review(action.Review):
    rewind: Rewind


def claim(app, origin, transport):
    attempt = action._attempt(app) if app is not None else None
    if attempt is None or not attempt.pending or not isinstance(attempt.review, Review):
        return None
    rewind = attempt.review.rewind
    if (not rewind.reverted or rewind.claimed or not action._authorized(app)
            or transport is not rewind.binding.transport or not origin or origin.operation_id != attempt.operation
            or origin.execution_id != (attempt.segments or [None])[-1]):
        raise ValueError("The reviewed Codex replacement lost its unique execution owner")
    rewind.connected(app)
    rewind.claimed = True
    return rewind


def save_user_item(app, index, metadata, input_items, turn, payload):
    if index is None or app is None or payload.get("threadId") != metadata["app_server_thread_id"] or payload.get("turnId") != turn:
        return
    if index >= len(app.conversation):
        return
    record = app.conversation[index]
    item = payload.get("item") or {}
    if (record.get("role") != "user" or record.get("provider_metadata") is not metadata
            or text_item(input_items) != record.get("content") or text_item(item.get("content")) != record.get("content")
            or item.get("type") != "userMessage"):
        return
    ident = item.get("id")
    if not isinstance(ident, str) or re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", ident) is None:
        return
    if "app_server_user_item_id" in metadata:
        return  # Rewind independently rejects turns containing multiple user items.
    metadata["app_server_user_item_id"] = ident
    app._edit(index, "Codex confirmed native user item")
