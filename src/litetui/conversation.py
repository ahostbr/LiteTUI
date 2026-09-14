"""The conversation STORE: where a conversation lives and how it is read.

Extracted from `app.LiteTUI` (finding 4). This half is the part with no
opinion about the UI -- pure functions over a transcript path and the
records inside it -- which is exactly why it is the half that can leave.
The app keeps the live message list and every widget; this module knows
only bytes on disk.

The names on `LiteTUI` are kept as delegating aliases rather than removed:
18 test files and the rest of the class call them, and a rename would have
been a behaviour change wearing a refactor's clothes. There is exactly one
copy of each implementation, and it is here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from litetui import paths, runtime_log

TRANSCRIPT_NAME = "convo.jsonl"

CONVO_SEED_FILES = {
    "memory.md": (
        "# Memory Index\n\n"
        "One line per memory, NEWEST AT THE TOP. Bodies live in "
        f"`{paths.MEMORIES_DIR}/`.\n\n"
        "`- [short title](memories/slug.md) — the hook`\n\n"
        "POINTERS ONLY. ~50 tokens (about 200 chars) per line, hard. Enough to\n"
        "decide whether to open the file, nothing more. If you are explaining\n"
        "the thing here, it belongs in the topic file instead.\n\n"
        "This file is injected into the system prompt ONCE, at the start of the\n"
        "conversation, so a long line permanently crowds out other entries.\n\n"
        "Append and edit only — never rewrite it to make it shorter. A line\n"
        "removed here orphans a file that nothing will ever open again.\n\n"
        "---\n\n"
    ),
    "soul.md": (
        "# Soul\n\n"
        "Who I am in this conversation. I write this for myself; it survives\n"
        "/resume and /compact when the transcript does not.\n\n"
        "## How the user works\n"
        "_Preferences, tone, what they want more or less of._\n\n"
        "## Standing corrections\n"
        "_Things I got wrong and was corrected on. The correction, and WHY —\n"
        "a rule without its reason gets re-litigated or misapplied._\n\n"
        "## How I work here\n"
        "_Habits that have proven useful in this conversation specifically._\n"
    ),
    "handoff.md": (
        "# Handoff\n\n"
        "Written so the next session can ACT without re-deriving anything.\n\n"
        "> Every row must be CHECKABLE: name the file, the command, or the\n"
        "> identifier. A query can be re-run; a bare claim can only be believed.\n"
        "> State when each row was last MEASURED — not when it was assumed.\n\n"
        "## 1. In flight\n"
        "_What is running or half-done right now. 'Nothing' is a valid and\n"
        "useful answer — say it explicitly rather than leaving the section out._\n\n"
        "## 2. Owed — split by owner\n"
        "_Mine / theirs / the user's. An unowned item is one nobody does._\n\n"
        "## 3. Absent by decision\n"
        "_What is deliberately NOT being done, and what defends that choice.\n"
        "Without this, the next session rediscovers it and redoes it._\n\n"
        "## 4. Caveats riding the green lines\n"
        "_What 'it works' does NOT cover. The limits of every pass claim._\n\n"
        "## 5. My corrections and retractions\n"
        "_What I claimed and later found wrong. Carry these forward: a\n"
        "retracted claim that is not written down comes back as fact._\n"
    ),
}


class ConversationRepository:
    """Reads conversation stores. Owns no widgets and no live state."""

    @staticmethod
    def read(path: Path) -> tuple[dict, list[dict]]:
        meta: dict = {}
        msgs: list[dict] = []
        with path.open("rb") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue  # a torn record may also end inside a UTF-8 character
                kind = rec.get("type")
                if kind == "meta":
                    meta = rec
                elif kind == "snapshot":
                    msgs = list(rec.get("messages") or [])
                elif kind == "rename":
                    # APPEND-ONLY, like every other record here. The name is not
                    # patched into the meta line -- rewriting a JSONL record in
                    # place is how a torn write loses the whole transcript, and
                    # the last rename simply wins on replay.
                    new_name = rec.get("name")
                    if isinstance(new_name, str):
                        cleaned = new_name.strip()
                        meta = {**meta, "name": cleaned} if cleaned else {
                            k: v for k, v in meta.items() if k != "name"
                        }
                elif kind == "edit":
                    i = rec.get("index")
                    if isinstance(i, int) and 0 <= i < len(msgs) and isinstance(
                        rec.get("message"), dict
                    ):
                        msgs[i] = rec["message"]
                elif kind == "truncate":
                    keep_from = rec.get("keep_from")
                    if not isinstance(keep_from, int) or keep_from < 0:
                        continue  # unreadable marker: leave the history intact
                    head = []
                    if (
                        rec.get("keep_system")
                        and msgs
                        and msgs[0].get("role") == "system"
                    ):
                        head = [msgs[0]]
                    prepend = [
                        m for m in (rec.get("prepend") or []) if isinstance(m, dict)
                    ]
                    msgs = head + prepend + msgs[keep_from:]
                elif kind == "msg" and isinstance(rec.get("message"), dict):
                    msgs.append(rec["message"])
        return meta, msgs

    @staticmethod
    def fmt_size(n: int) -> str:
        for unit, div in (("MB", 1024 * 1024), ("KB", 1024)):
            if n >= div:
                return f"{n / div:.1f}{unit}"
        return f"{n}B"

    @staticmethod
    def label(meta: dict, msgs: list[dict]) -> str:
        """What a conversation is CALLED in any listing.

        A name given with /rename wins over the derived first-user-message
        preview -- that is the whole point of naming one. Both /convos and the
        /resume picker call this, because they already render the same column
        and their own comment says the two must not drift.
        """
        name = str((meta or {}).get("name") or "").strip()
        if name:
            return name
        return ConversationRepository.title(msgs)

    @staticmethod
    def title(msgs: list[dict]) -> str:
        for m in msgs:
            if m.get("role") != "user":
                continue
            c = ConversationRepository.flatten(m.get("content")).replace("\n", " ")
            if c:
                return c[:60] + ("\u2026" if len(c) > 60 else "")
        return "(no user message)"

    @staticmethod
    def flatten(content) -> str:
        if isinstance(content, list):  # image turn: [{image_url...}, {text...}]
            text = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("text")
            )
            return ("[image] " + text).strip()
        return (content or "").strip()

    @staticmethod
    def list_all() -> list[tuple[Path, dict, list[dict]]]:
        """Returns (transcript_path, meta, messages) newest first."""
        if not paths.CONVO_DIR.exists():
            return []
        out = []
        for d in paths.CONVO_DIR.iterdir():
            if not d.is_dir():
                continue
            p = d / TRANSCRIPT_NAME
            if not p.exists():
                continue
            try:
                meta, msgs = ConversationRepository.read(p)
            except OSError:
                continue
            out.append((p, meta, msgs))
        out.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
        return out

    # ── identity and lifecycle ───────────────────────────────────────────
    # An instance owns ONE conversation store. The class-level readers above
    # stay static: they answer questions about any path, instance or not.

    def __init__(self, on_error=None):
        self.convo_id: str = ""
        self.convo_dir: Path | None = None
        self.convo_path: Path | None = None
        # STAGED but not born: an id and paths are assigned, nothing is on
        # disk. Booting to run /resume used to mint a throwaway conversation
        # first, so the list you opened /resume to read filled with the debris
        # of opening it.
        self.pending = False
        self.loading = False   # replaying from disk; suppress writes
        self.persist_error: str | None = None
        # How a persistence failure reaches a human. The store cannot know --
        # it has no widgets -- so the app hands it a way to speak.
        self._on_error = on_error
        self._lease = None

    def stage(self, convo_id: str) -> None:
        """Pick the id and the paths. Touch no disk."""
        self.release()
        self.convo_id = convo_id
        self.convo_dir = paths.CONVO_DIR / convo_id
        self.convo_path = self.convo_dir / TRANSCRIPT_NAME
        self.pending = True

    def adopt(self, path: Path, convo_id: str) -> None:
        """Repoint at an existing store (resume).

        The staged conversation is abandoned WITHOUT being written -- that is
        the point of staging. Clearing the flag before reassigning the paths
        also stops a later write from materialising the RESUMED store as if it
        were new.
        """
        self.acquire(path.parent)
        self.pending = False
        self.convo_path = path
        self.convo_dir = path.parent
        self.convo_id = convo_id

    @property
    def owned(self) -> bool:
        return self._lease is not None and self._lease.handle is not None

    def acquire(self, directory: Path | None = None) -> None:
        from litetui.shared_state import Lease, check_data_version
        directory = directory or self.convo_dir
        if directory is None:
            return
        target = directory / ".session.lease"
        if self._lease is not None and self._lease.path == target:
            return
        check_data_version(directory.parent.parent)
        lease = Lease(target).acquire()
        self.release()
        self._lease = lease

    def release(self) -> None:
        if getattr(self, "_lease", None) is not None:
            self._lease.release()
            self._lease = None

    def note_error(self, e: Exception) -> str | None:
        """Record the first persistence failure. Returns the text to show, once.

        Only the FIRST is reported: a store that has gone away fails on every
        subsequent write, and repeating it would bury the conversation the user
        is trying to keep having.
        """
        if self.persist_error is not None:
            return None
        self.persist_error = f"{type(e).__name__}: {e}"
        return self.persist_error

    def seed(self) -> None:
        """Create the store directory and its seed files. Never clobbers."""
        if self.convo_dir is None:
            return
        try:
            self.acquire()
            (self.convo_dir / paths.MEMORIES_DIR).mkdir(parents=True, exist_ok=True)
            for fname, seed in CONVO_SEED_FILES.items():
                f = self.convo_dir / fname
                if not f.exists():  # never clobber a resumed store
                    f.write_text(seed, encoding="utf-8")
        except OSError as e:
            self._raise_to_app(e)

    def _raise_to_app(self, e: Exception) -> None:
        msg = self.note_error(e)
        if msg is not None:
            runtime_log.record(
                "persistence_failure",
                site="conversation.repository",
                component="conversation",
                error_type=type(e).__name__,
            )
            if self._on_error is not None:
                self._on_error(msg)

    # ── the record layer ─────────────────────────────────────────────────
    # One writer. Every record shape below funnels into write_record, so a
    # store that has gone away is reported from exactly one place.

    def write_record(self, rec: dict) -> None:
        if self.convo_path is None or self.loading:
            return
        # Staged but not born: keep it in memory. The caller snapshots the live
        # list when it creates the file, so nothing written here would have been
        # lost -- and NOT writing is the entire point, otherwise the boot-time
        # system prompt creates the directory it was meant to avoid creating.
        if self.pending:
            return
        try:
            self.acquire()
            self.convo_path.parent.mkdir(parents=True, exist_ok=True)
            payload = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8")
            with self.convo_path.open("a+b") as f:
                # Never overwrite crash evidence. A missing newline must not
                # concatenate the next successful record onto the torn one.
                f.seek(0, 2)
                if f.tell():
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        payload = b"\n" + payload
                f.write(payload)
        except OSError as e:
            # Surface it once. A persistence layer that fails silently is worse
            # than none at all: you find out at /resume, when it is too late.
            self._raise_to_app(e)

    def record_msg(self, msg: dict, *, usage: dict | None = None,
                   model: str | None = None) -> None:
        self.write_record({"type": "msg", "ts": time.time(), "message": msg,
                           **({"usage": usage, "model": model} if usage is not None else {})})

    def record_snapshot(self, messages: list[dict], reason: str = "") -> None:
        """Full-list record. Kept for readability of older files; prefer edit."""
        self.write_record({
            "type": "snapshot", "ts": time.time(),
            "reason": reason, "messages": messages,
        })

    def record_edit(self, index: int, message: dict, reason: str = "") -> None:
        """Record an in-place change to ONE message.

        /system and the tools toggle only ever rewrite message 0, which is the
        biggest message in the file (system prompt + store block + tools). A
        snapshot for that wrote the entire conversation to disk to record a
        one-message change.
        """
        self.write_record({
            "type": "edit", "ts": time.time(), "reason": reason,
            "index": index, "message": message,
        })

    def record_truncate(self, keep_from: int, prepend: list[dict],
                        keep_system: bool, reason: str = "", *,
                        measurements: dict | None = None) -> None:
        """Record 'drop the head, splice these in front of what remains'."""
        self.write_record({
            "type": "truncate", "ts": time.time(), "reason": reason,
            "keep_from": keep_from, "keep_system": keep_system,
            "prepend": prepend,
            **({"measurements": measurements} if measurements is not None else {}),
        })

    def record_meta(self, model: str, seat_name=None, seat_id=None) -> None:
        """The owning seat is written at CREATION, so /resume can say whose
        conversation this was -- the seat may be renamed or re-registered
        later, and the answer wanted is who owned it THEN."""
        self.write_record({
            "type": "meta", "v": 3, "id": self.convo_id,
            "created": time.time(), "model": model,
            **({"agent_name": seat_name} if seat_name is not None else {}),
            **({"agent_id": seat_id} if seat_id is not None else {}),
        })
