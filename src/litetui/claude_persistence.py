"""Durable delivery ledger for the Claude-native backend (claude-persistence).

Companion JSON file in the conversation directory, sibling to `convo.jsonl` and
`settings.json` — the same placement and the same reasons as `convo_settings`:
the directory is already the unit /resume carries, and mutable state does not
belong in an append-only transcript.

WHAT THIS FILE IS FOR, AND WHAT IT IS NOT. The provider transcript stays
authoritative for execution and context (plan 3.1). This ledger records only
what LiteTUI must be able to answer after a crash:

  * which explicit Claude SEGMENT this conversation last selected, and which
    native session id and workspace that segment is bound to;
  * for every user input accepted while busy, a durable record written BEFORE
    LiteTUI acknowledges acceptance, so an input can never be lost between the
    keypress and the send.

🔴 A SUCCESSFUL SDK WRITE IS NOT PROOF THE PROVIDER RAN THE TURN. That is the
whole reason the delivery chain has four states instead of a boolean:

    prepared -> submitted -> acknowledged -> terminal

`prepared` is ours alone and safe to send. `submitted` means the bytes left;
`acknowledged` means the SDK gave evidence it arrived. Neither proves the turn
did or did not execute — so ON EVERY LOAD both become `uncertain`, carrying
`uncertain_from` so the UI can say which one it was. An uncertain entry is
NEVER replayed automatically; resolving it is an explicit user decision
(plan 2.5, 3.1). The load-time rewrite is in memory only: reading a ledger
never writes one, and because the rule is re-derived on every load it survives
any number of restarts without a save in between.

⚠️ PENDING ENTRIES CANNOT MIGRATE BETWEEN SEGMENTS, and that is structural
rather than checked: an entry is stored INSIDE its segment, so there is no
field to point it at a different one. `prepare` additionally refuses an
`operation_id` that already exists under another segment, because the only way
to reach that call is a caller that lost track of which segment it queues for.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

LEDGER_NAME = "claude_ledger.json"

#: Bumped when a stored document stops being readable by this code. An unknown
#: version is an error, never a silent partial read — `check_data_version` in
#: shared_state.py made the same call for the same reason.
SCHEMA_VERSION = 1

PREPARED = "prepared"
SUBMITTED = "submitted"
ACKNOWLEDGED = "acknowledged"
TERMINAL = "terminal"
UNCERTAIN = "uncertain"

#: Which states a caller may move an entry to. `prepared -> uncertain` is
#: deliberately absent: an input that never left is not ambiguous, it is simply
#: still queued, and blurring the two is what would license a silent replay.
TRANSITIONS: dict[str, frozenset[str]] = {
    PREPARED: frozenset({SUBMITTED, TERMINAL}),
    SUBMITTED: frozenset({ACKNOWLEDGED, TERMINAL, UNCERTAIN}),
    ACKNOWLEDGED: frozenset({TERMINAL, UNCERTAIN}),
    UNCERTAIN: frozenset({TERMINAL}),
    TERMINAL: frozenset(),
}

#: The states a restart cannot vouch for; see the module docstring.
IN_FLIGHT = (SUBMITTED, ACKNOWLEDGED)

#: Metadata keys `update_delivery` refuses: structure, not delivery detail.
#: `id` and `state` are not here because they are named parameters of that
#: method, so `**metadata` cannot carry them at all — Python raises first.
RESERVED = frozenset({"segment_id"})


class LedgerError(RuntimeError):
    """A ledger that cannot be trusted, or a caller asking for the impossible.

    Raised rather than recovered from on purpose. `convo_settings.load` treats
    an unreadable file as absent because losing a model preference is cheaper
    than refusing to open a transcript; here the file IS the record of what was
    sent, so answering "no deliveries" for a file we failed to parse would
    invent the one fact the caller must not get wrong.
    """


def path_for(convo_dir: Path | str) -> Path:
    return Path(convo_dir) / LEDGER_NAME


class ClaudeLedger:
    """Segment selection and delivery records for one conversation.

    `path` is the CONVERSATION DIRECTORY, not the ledger file — same shape as
    `convo_settings.load(convo_dir)`.
    """

    def __init__(self, path: Path | str) -> None:
        self.dir = Path(path)
        if self.dir.is_file():
            raise LedgerError(
                f"{self.dir} is a file; ClaudeLedger takes the conversation "
                f"directory that holds {LEDGER_NAME}"
            )
        self.file = path_for(self.dir)
        self._data = self._load()

    # ---- reads -----------------------------------------------------------

    @property
    def selected(self) -> dict[str, Any] | None:
        """The segment this conversation last selected, or None if never.

        A copy, including its `entries`: callers render it, and a handed-out
        reference to live ledger state is a mutation nobody wrote down.
        """
        return self.segment(self._data["selected_segment"])

    def segment(self, id: str | None) -> dict[str, Any] | None:
        found = self._data["segments"].get(id) if id else None
        return deepcopy(found) if found is not None else None

    def pending(self, segment_id: str) -> list[dict[str, Any]]:
        """Every non-terminal entry for the segment, in the order prepared.

        🔴 NOT A SEND QUEUE. This includes `uncertain` entries by OpenBolt's
        decision (2026-09-23) so one call can drive the held-input UI — which
        means THE CALLER filters. Submit entries whose state is `prepared`;
        anything else has already left once and re-sending it is the duplicate
        side effect the whole ledger exists to prevent.
        """
        segment = self._require_segment(segment_id)
        return [deepcopy(e) for e in segment["entries"] if e["state"] != TERMINAL]

    # ---- writes ----------------------------------------------------------

    def select_segment(self, workspace: str, new: bool = False,
                       seed: str | None = None) -> dict[str, Any]:
        """Resume the selected segment for `workspace`, or start a fresh one.

        A DIFFERENT WORKSPACE GETS A DIFFERENT SEGMENT, even with `new=False`.
        Resume is by exact native id AND workspace (plan 3.1); reusing a
        segment whose recorded workspace no longer matches would resume a
        native session against a directory it was not created in.

        `seed`: a LiteTUI compaction summary this segment's sessions carry in
        their system prompt (claude_backend.seeded_append), on every open and
        every resume. Saved with the segment, so it survives a restart.
        """
        current = self._data["segments"].get(self._data["selected_segment"] or "")
        if not new and current is not None and current["workspace"] == workspace:
            return deepcopy(current)
        segment: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "session_id": None,
            "workspace": workspace,
            "created_at": time.time(),
            "entries": [],
        }
        if seed:
            segment["seed"] = seed
        self._data["segments"][segment["id"]] = segment
        self._data["selected_segment"] = segment["id"]
        self._save()
        return deepcopy(segment)

    def bind_session(self, segment_id: str, session_id: str) -> dict[str, Any]:
        """Record the native session id, which arrives after the segment does.

        Rebinding the same id is a no-op; rebinding a DIFFERENT one is refused.
        A reconnect assigns a new runtime generation, not a new native session
        (plan 2.5) — so a second id means a second session, and a second
        session means a new segment, or the display records of two native
        transcripts silently merge into one.
        """
        segment = self._require_segment(segment_id)
        if not session_id:
            raise LedgerError(f"Segment {segment_id} needs a native session id")
        bound = segment["session_id"]
        if bound == session_id:
            return deepcopy(segment)
        if bound is not None:
            raise LedgerError(
                f"Segment {segment_id} is bound to native session {bound}; "
                f"session {session_id} needs its own segment"
            )
        segment["session_id"] = session_id
        self._save()
        return deepcopy(segment)

    def fix_system_prompt(self, segment_id: str, text: str) -> str:
        """Record the segment's system prompt the first time, and return the recorded one.

        Fixed for the life of the segment, like its seed: every open and every resume
        of its native session carries the same prefix, so the prompt cache holds, and
        a store change reaches Claude at the next session rather than mid-session.
        """
        segment = self._require_segment(segment_id)
        if not segment.get("system_prompt"):
            segment["system_prompt"] = text
            self._save()
        return segment["system_prompt"]

    def note_cache(self, segment_id: str, used_at: float, model: str | None) -> None:
        """Remember when this segment last read or wrote Claude's prompt cache, and
        with which model, so a resume after a restart can tell warm from cold (T911)."""
        segment = self._require_segment(segment_id)
        segment["cache_used_at"] = used_at
        segment["cache_model"] = model
        self._save()

    def prepare(
        self,
        segment_id: str,
        content: str,
        profile: str,
        source: str,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Durably record an accepted input BEFORE anything is sent.

        `operation_id` is the caller's correlation id and makes this
        idempotent: preparing it twice returns the first entry rather than
        queueing the same input again ("Busy input: queued once", plan 5).
        """
        segment = self._require_segment(segment_id)
        if operation_id is not None:
            for owner in self._data["segments"].values():
                for existing in owner["entries"]:
                    if existing["operation_id"] != operation_id:
                        continue
                    if owner["id"] != segment_id:
                        raise LedgerError(
                            f"Operation {operation_id} is already prepared on "
                            f"segment {owner['id']}; a pending entry cannot "
                            f"move to segment {segment_id}"
                        )
                    return deepcopy(existing)
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "segment_id": segment_id,
            "state": PREPARED,
            "content": content,
            "profile": profile,
            "source": source,
            "operation_id": operation_id,
            "prepared_at": time.time(),
        }
        segment["entries"].append(entry)
        self._save()
        return deepcopy(entry)

    def update_delivery(self, id: str, state: str, **metadata: Any) -> dict[str, Any]:
        """Advance one entry along the delivery chain, recording `metadata`.

        Metadata is where the native identities, the effective model and any
        failure detail live; it may not rewrite the entry's `segment_id`.
        """
        entry = self._find_entry(id)
        if entry is None:
            raise LedgerError(f"No delivery {id} in {self.file}")
        allowed = TRANSITIONS.get(entry["state"])
        if allowed is None:
            raise LedgerError(f"Delivery {id} holds unknown state {entry['state']!r}")
        if state not in allowed:
            raise LedgerError(
                f"Delivery {id} cannot move from {entry['state']} to {state}; "
                f"allowed from here: {', '.join(sorted(allowed)) or 'nothing'}"
            )
        clashes = RESERVED & set(metadata)
        if clashes:
            raise LedgerError(
                f"Delivery metadata may not set {', '.join(sorted(clashes))}"
            )
        entry.update(metadata)
        if state == UNCERTAIN:
            # Same field the load-time rule writes, so an uncertain entry always
            # says which state it was in when the answer stopped coming —
            # whether a reader failure marked it live or a restart derived it.
            entry["uncertain_from"] = entry["state"]
        entry["state"] = state
        entry[f"{state}_at"] = time.time()
        self._save()
        return deepcopy(entry)

    # ---- storage ---------------------------------------------------------

    def _require_segment(self, segment_id: str) -> dict[str, Any]:
        segment = self._data["segments"].get(segment_id)
        if segment is None:
            raise LedgerError(f"No Claude segment {segment_id} in {self.file}")
        return segment

    def _find_entry(self, id: str) -> dict[str, Any] | None:
        # ponytail: linear scan. A conversation holds a handful of held inputs;
        # index by id if one ever holds thousands.
        for segment in self._data["segments"].values():
            for entry in segment["entries"]:
                if entry["id"] == id:
                    return entry
        return None

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {
                "schema_version": SCHEMA_VERSION,
                "selected_segment": None,
                "segments": {},
            }
        except (OSError, ValueError) as exc:
            raise LedgerError(
                f"Claude ledger {self.file} is unreadable: {exc}"
            ) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("segments"), dict):
            raise LedgerError(f"Claude ledger {self.file} is not a ledger document")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise LedgerError(
                f"Claude ledger {self.file} has schema version "
                f"{raw.get('schema_version')!r}; this runtime reads {SCHEMA_VERSION}"
            )
        for id, segment in raw["segments"].items():
            if not isinstance(segment, dict) or not isinstance(
                segment.get("entries"), list
            ):
                raise LedgerError(
                    f"Claude ledger {self.file} has a malformed segment {id}"
                )
            for entry in segment["entries"]:
                if entry.get("state") in IN_FLIGHT:
                    entry["uncertain_from"] = entry["state"]
                    entry["state"] = UNCERTAIN
        return raw

    def _save(self) -> None:
        """Temp file plus one `os.replace`, under the shared write lease.

        NEVER truncate-then-write. `write_text` truncates first, so a reader
        landing between the two syscalls sees an empty file — and `_load`
        answers an unparseable ledger with `LedgerError`, which would turn a
        concurrent read into a recovery error for a conversation that is fine.
        A failed replace leaves the previous ledger exactly as it was.
        """
        from litetui.shared_state import coordinated_write

        self.dir.mkdir(parents=True, exist_ok=True)
        with coordinated_write(self.file):
            fd, tmp = tempfile.mkstemp(
                dir=str(self.dir), prefix=".claude_ledger-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    text = json.dumps(self._data, indent=2, ensure_ascii=False)
                    fh.write(text + "\n")
                os.replace(tmp, self.file)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
