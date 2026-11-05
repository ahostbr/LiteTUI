"""Fork a verified linear Claude transcript without modifying its original session."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui.backend_session import SessionState
from litetui.claude_compaction_evidence import Binding, _document, _native_id

LIMIT = 1024 * 1024
MAX_TURNS = 32


def _hash(value):
    return hashlib.sha256(projection.encoded(value)).hexdigest()


def _read(session, workspace):
    from claude_agent_sdk._internal.sessions import (
        _canonicalize_path,
        _find_project_dir,
    )

    if not _native_id(session):
        raise ValueError("Invalid Claude session UUID")
    directory = _find_project_dir(_canonicalize_path(workspace))
    if directory is None:
        raise ValueError("Claude transcript is unavailable in this workspace")
    file = directory / (session + ".jsonl")
    if file.is_symlink() or file.resolve().parent != directory.resolve():
        raise ValueError("Claude transcript must be stored in its owned workspace")
    with file.open("rb") as handle:
        raw = handle.read(LIMIT + 1)
    if not raw or len(raw) > LIMIT or not raw.endswith(b"\n"):
        raise ValueError("Claude Edit/Retry requires a complete native transcript within 1 MiB")
    native, seen = [], set()
    for line in raw.splitlines():
        row = json.loads(line)
        kind = row.get("type")
        if kind in {"custom-title", "ai-title", "last-prompt", "file-history-snapshot"}:
            continue
        ident = row.get("uuid")
        prev = native[-1]["uuid"] if native else None
        if (
            kind not in {"user", "assistant"}
            or row.get("sessionId") != session
            or row.get("isSidechain")
            or row.get("isMeta")
            or any(row.get(k) for k in ("teamName", "agentName", "sourceToolAssistantUUID"))
            or row.get("logicalParentUuid")
            or row.get("parentUuid") != prev
            or not _native_id(ident)
            or ident in seen
        ):
            raise ValueError(
                "Claude Edit/Retry requires a linear top-level transcript with no hidden context changes"
            )
        seen.add(ident)
        native.append(row)
        if len(native) > MAX_TURNS * 2:
            raise ValueError("Claude Edit/Retry supports up to 32 complete plain turns")
    if len(native) % 2 or not native:
        raise ValueError("Claude history ends outside a complete turn")
    for user, reply in zip(native[::2], native[1::2], strict=True):
        if (
            user["type"] != "user"
            or reply["type"] != "assistant"
            or not isinstance(_plain(user), str)
            or not isinstance(_plain(reply), str)
            or not isinstance(reply["message"].get("id"), str)
        ):
            raise ValueError(
                "Claude Edit/Retry requires one plain user and one completed assistant reply per turn"
            )
    from claude_agent_sdk import get_session_messages

    visible = get_session_messages(session, directory=workspace)
    if len(visible) != len(native) or any(
        m.session_id != session or m.uuid != row["uuid"] or m.message != row["message"]
        for m, row in zip(visible, native, strict=True)
    ):
        raise ValueError("Claude SDK history disagrees with the saved native message chain")
    return raw, native


def _plain(row):
    message = row.get("message") or {}
    if message.get("role") != row["type"]:
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    pieces = []
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            pieces.append(block["text"])
        elif row["type"] != "assistant" or block.get("type") not in {"thinking", "redacted_thinking"}:
            return None
    return "".join(pieces)


def _check_prompt(session, directory, text):
    from litetui.claude_turn import SYSTEM_PROMPT_FILE

    path = Path(directory) / SYSTEM_PROMPT_FILE
    options = session.options.system_prompt
    if (
        not isinstance(options, dict)
        or options.get("type") != "file"
        or Path(options.get("path") or "") != path
        or path.is_symlink()
        or path.resolve().parent != directory.resolve()
    ):
        raise ValueError("Claude SDK did not load the reviewed conversation's system prompt")
    with path.open("rb") as handle:
        data = handle.read(LIMIT + 1)
    if len(data) > LIMIT or data != text.encode("utf-8"):
        raise ValueError("Claude SDK prompt bytes disagree with the reviewed segment")


def _saved_pair(messages, index, user, reply, ledger, current):
    before, after = messages[index : index + 2]
    delivery = before.get("claude_delivery") or {}
    meta = after.get("claude_native") or {}
    segment = ledger["segments"].get(delivery.get("segment_id")) or {}
    entry = next((v for v in segment.get("entries", ()) if v["id"] == delivery.get("id")), None)
    if (
        not entry
        or entry["state"] != "terminal"
        or entry.get("stop_reason") != "stop"
        or entry["content"] != before.get("content")
        or after.get("role") != "assistant"
        or meta.get("delivery_id") != entry["id"]
        or meta.get("segment_id") != segment.get("id")
        or meta.get("session_id") != segment.get("session_id")
        or meta.get("message_ids") != [reply["message"]["id"]]
        or meta.get("activities")
        or after.get("content") != _plain(reply)
    ):
        raise ValueError("The saved Claude turn lacks a matching completed native reply and delivery")
    native_text = (entry["store_notice"] + "\n\n" if entry.get("store_notice") else "") + entry["content"]
    if _plain(user) != native_text:
        raise ValueError("Saved Claude input disagrees with the native user message")
    if (
        segment["session_id"] != current
        and (user.get("forkedFrom") or {}).get("sessionId") != segment["session_id"]
    ):
        raise ValueError("The selected Claude turn belongs to another native segment")


def _semantics(entry):
    ignored = {
        "uuid",
        "parentUuid",
        "sessionId",
        "timestamp",
        "logicalParentUuid",
        "isSidechain",
        "forkedFrom",
        "teamName",
        "agentName",
        "slug",
        "sourceToolAssistantUUID",
    }
    return {k: v for k, v in entry.items() if k not in ignored}


@dataclass
class Fork:
    binding: Binding
    raw: bytes
    rows: list
    cut: int
    plan: dict
    new_session: str | None = None
    sdk_owner: object | None = None
    terminal: str | None = None
    stage: str = "native fork creation"

    @classmethod
    def review(cls, app, messages, selected):
        binding = Binding.capture(app, rewrite=True)
        prior = binding.before["segments"][binding.segment]
        if (
            not prior.get("system_prompt")
            or type(prior.get("store_injected")) is not bool
            or "store_identity" not in prior
        ):
            raise ValueError(
                "Claude Edit/Retry requires a segment with the original system prompt and store recorded"
            )
        if getattr(app, "_store_notice_text", None) and not binding.ledger.store_notice_delivered:
            raise ValueError(
                "Send the pending store notice in an ordinary turn before forking Claude history"
            )
        if getattr(binding.session.options, "env", {}).get("CLAUDE_CONFIG_DIR"):
            raise ValueError(
                "Claude fork requires the native session and offline SDK to share their configured transcript directory"
            )
        _check_prompt(binding.session, binding.ledger.dir, prior["system_prompt"])
        original, native = _read(binding.native_session, binding.workspace)
        tagged = [i for i, msg in enumerate(messages) if msg.get("role") == "user"]
        if (
            len(tagged) * 2 != len(native)
            or selected not in tagged
            or any(i + 1 >= len(messages) for i in tagged)
        ):
            raise ValueError("Saved Claude messages do not cover the complete native session")
        first = tagged.index(selected)
        if tagged[first:] != list(range(selected, len(messages), 2)) or len(messages) != selected + 2 * (len(tagged) - first):
            raise ValueError("Claude Edit/Retry requires every excluded saved row to match a native user/assistant pair")
        if first == 0:
            raise ValueError(
                "The first native Claude turn has no prior fork checkpoint. Start a new conversation."
            )
        suffix = []
        for n, i in enumerate(tagged):
            user, reply = native[2 * n : 2 * n + 2]
            _saved_pair(messages, i, user, reply, binding.before, binding.native_session)
            if n >= first:
                suffix.append(
                    {
                        "turn_id": reply["uuid"],
                        "user_item_id": user["uuid"],
                        "text": _plain(user),
                        "items": [
                            {"id": user["uuid"], "kind": "userMessage", "details": ""},
                            {"id": reply["uuid"], "kind": "agentMessage", "details": _plain(reply)},
                        ],
                    }
                )
        if suffix[0]["text"] != messages[selected]["content"]:
            raise ValueError("Claude Edit/Retry cannot replace a prompt carrying a one-time store notice")
        info = {
            "old_segment": binding.segment,
            "new_segment": uuid.uuid4().hex,
            "delivery": uuid.uuid4().hex,
            "checkpoint": native[2 * first - 1]["uuid"],
            "ledger_before": _hash(binding.before),
        }
        plan = {
            "thread_id": binding.native_session,
            "before_sha256": hashlib.sha256(original).hexdigest(),
            "prefix_sha256": _hash(native[: 2 * first]),
            "retained_turns": first,
            "suffix": suffix,
            "fork": info,
        }
        if len(projection.encoded(plan)) > 65536:
            raise ValueError("Claude fork exceeds the 64 KiB visible review limit")
        return cls(binding, original, native, 2 * first, plan)

    def candidate(self, text):
        ids = self.plan["fork"]
        return {
            "role": "user",
            "content": text,
            "claude_delivery": {"id": ids["delivery"], "segment_id": ids["new_segment"], "state": "prepared"},
        }

    def check_old(self, app):
        self.binding.check(app, rewrite=True)
        _check_prompt(
            self.binding.session,
            self.binding.ledger.dir,
            self.binding.before["segments"][self.binding.segment]["system_prompt"],
        )
        if _read(self.binding.native_session, self.binding.workspace)[0] != self.raw:
            raise ValueError("The native Claude transcript changed after review")

    def fork_prefix(self):
        _, rows = _read(self.new_session, self.binding.workspace)
        prefix = rows[: self.cut]
        old_ids = {r["uuid"] for r in self.rows}
        if len(prefix) != self.cut:
            raise ValueError("Claude fork omitted the reviewed prefix")
        for old, new in zip(self.rows[: self.cut], prefix, strict=True):
            if (
                new["uuid"] in old_ids
                or _semantics(old) != _semantics(new)
                or new.get("forkedFrom")
                != {"sessionId": self.binding.native_session, "messageUuid": old["uuid"]}
            ):
                raise ValueError("Claude fork did not preserve the reviewed native message chain")
        return rows

    async def prepare(self, app, attempt, item, before_delivery):
        from claude_agent_sdk import fork_session

        self.check_old(app)
        if app._stop_requested:
            raise ValueError("Stopped before forking Claude history")
        b, ids = self.binding, self.plan["fork"]
        attempt.mutated = True
        child = fork_session(b.native_session, directory=b.workspace, up_to_message_id=ids["checkpoint"])
        self.new_session = child.session_id
        if not _native_id(self.new_session) or self.new_session == b.native_session:
            raise ValueError("Claude returned an invalid fork identity")
        if len(self.fork_prefix()) != self.cut:
            raise ValueError("Claude fork contains unreviewed history")
        self.check_old(app)
        self.stage = "retiring the original SDK session after fork creation"
        await app.backend.close()
        b.retired(app)
        if _read(b.native_session, b.workspace)[0] != self.raw or _document(app, b.ledger) != b.before:
            raise ValueError("The original Claude session or ledger changed during retirement")
        action._stable(app, attempt.review, original=True)
        if app._stop_requested:
            raise ValueError("Stopped after Claude fork creation. Inspect the sessions before continuing.")
        self.stage = "selecting the new segment and preparing its delivery"
        previous = b.before["segments"][b.segment]
        seed = previous.get("seed")
        new = b.ledger.select_segment(b.workspace, new=True, seed=seed, segment_id=ids["new_segment"])
        if previous.get("system_prompt"):
            b.ledger.fix_system_prompt(
                new["id"],
                previous["system_prompt"],
                store_identity=previous.get("store_identity"),
                store_injected=previous.get("store_injected"),
            )
        b.ledger.bind_session(new["id"], self.new_session)
        before_delivery()
        entry = b.ledger.prepare(
            new["id"],
            item["content"],
            attempt.review.profile,
            "rpc",
            operation_id=attempt.operation,
            delivery_id=ids["delivery"],
        )
        self.check_ledger(app, attempt, "prepared")
        self.stage = "publishing the local transcript after segment selection"
        item.update(_claude_entry=entry, _claude_segment=new["id"], _claude_conversation=app.convo_id)

    def check_ledger(self, app, attempt, state):
        b, ids = self.binding, self.plan["fork"]
        doc = _document(app, b.ledger)
        old = {
            **doc,
            "selected_segment": b.segment,
            "segments": {k: v for k, v in doc["segments"].items() if k != ids["new_segment"]},
        }
        segment = doc["segments"].get(ids["new_segment"], {})
        entries = segment.get("entries", [])
        allowed = {
            "id",
            "created_at",
            "session_id",
            "workspace",
            "entries",
            "seed",
            "system_prompt",
            "store_identity",
            "store_injected",
            "cache_used_at",
            "cache_model",
        }
        if (
            set(segment) - allowed
            or segment.get("id") != ids["new_segment"]
            or old != b.before
            or doc["selected_segment"] != ids["new_segment"]
            or segment.get("workspace") != b.workspace
            or segment.get("session_id") != self.new_session
            or segment.get("seed") != b.before["segments"][b.segment].get("seed")
            or any(
                segment.get(k) != b.before["segments"][b.segment].get(k)
                for k in ("system_prompt", "store_injected", "store_identity")
            )
            or len(entries) != 1
            or entries[0].get("id") != ids["delivery"]
            or entries[0].get("state") != state
            or entries[0].get("operation_id") != attempt.operation
            or entries[0].get("source") != "rpc"
            or entries[0].get("profile") != attempt.review.profile
            or entries[0]["content"] != json.loads(attempt.review.wire)["text"]
        ):
            raise ValueError("Claude fork selection or delivery failed independent ledger readback")
        return doc

    def before_query(self, app, origin, session, item):
        attempt = action._required_attempt(app)
        action._stable(app, attempt.review, admitted=True)
        if (
            app._stop_requested
            or attempt.stopped
            or not action._authorized(app)
            or not origin
            or origin.operation_id != attempt.operation
            or origin.execution_id != (attempt.segments or [None])[-1]
            or self.sdk_owner is not None
            or session is not app.backend.session
            or app.backend.segment_id != self.plan["fork"]["new_segment"]
            or session.options.resume != self.new_session
            or str(session.options.cwd) != self.binding.workspace
            or session.options.tools != []
            or session.options.mcp_servers != {}
            or not session.options.strict_mcp_config
            or item["_claude_entry"]["id"] != self.plan["fork"]["delivery"]
            or _read(self.binding.native_session, self.binding.workspace)[0] != self.raw
            or len(self.fork_prefix()) != self.cut
        ):
            raise ValueError("Claude replacement lost its reviewed fork, prompt or unique SDK owner")
        self.check_ledger(app, attempt, "prepared")
        _check_prompt(
            session,
            self.binding.ledger.dir,
            self.binding.before["segments"][self.binding.segment]["system_prompt"],
        )
        self.sdk_owner = session._owner

    def result(self, app, session, raw, event):
        if (
            session._owner is not self.sdk_owner
            or self.terminal is not None
            or type(raw).__name__ != "ResultMessage"
            or raw.is_error is not False
            or event.is_error
            or raw.subtype != "success"
            or raw.terminal_reason not in (None, "completed")
            or raw.session_id != self.new_session
            or event.session_id != self.new_session
            or not _native_id(raw.uuid)
            or raw.uuid != event.uuid
        ):
            raise ValueError("Claude replacement result changed its session or SDK owner")
        self.terminal = raw.uuid

    def settled(self, app, attempt, messages, selected):
        b = self.binding
        if (
            _read(b.native_session, b.workspace)[0] != self.raw
            or not self.terminal
            or not self.sdk_owner
            or app.backend.session is None
            or app.backend.session._owner is not self.sdk_owner
            or app.backend.session.session_id != self.new_session
            or self.sdk_owner.done()
            or app.backend.session.lifecycle.state != SessionState.READY
            or app.backend.session.lifecycle.active_turn is not None
            or app.backend.session.lifecycle.failure
            or app.backend.session.lifecycle.cleanup_errors
            or not b.owner.done()
            or b.session.lifecycle.state != SessionState.DISCONNECTED
            or b.session.lifecycle.cleanup_errors
        ):
            raise ValueError("The original Claude history or replacement SDK session changed")
        doc = self.check_ledger(app, attempt, "terminal")
        rows = self.fork_prefix()
        original_ids = {row["uuid"] for row in self.rows}
        if (
            len(rows) != self.cut + 2
            or len(messages) != selected + 2
            or self.terminal in original_ids
            or any(row["uuid"] in original_ids for row in rows[-2:])
        ):
            raise ValueError("Claude fork does not end after exactly one replacement")
        _saved_pair(messages, selected, *rows[-2:], doc, self.new_session)
        return {
            "thread_id": b.native_session,
            "session_id": self.new_session,
            "result_id": self.terminal,
            "prefix_sha256": self.plan["prefix_sha256"],
            "history_sha256": _hash(rows),
            "ledger_after": _hash(doc),
            "turn_id": rows[-1]["uuid"],
            "user_item_id": rows[-2]["uuid"],
        }


@dataclass(frozen=True)
class Review(action.Review):
    fork: Fork


def active(app):
    attempt = action._attempt(app)
    return (
        attempt.review.fork
        if attempt is not None and attempt.pending and isinstance(attempt.review, Review)
        else None
    )
