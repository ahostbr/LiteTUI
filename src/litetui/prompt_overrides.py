"""Conversation-scoped prompt text, not a replacement for host authority.

Only the enumerated ordinary prompts vary. Storage checks reject linked paths;
these are location/ownership checks, not a sandbox against the same OS user.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from litetui.agent_store import StoreError, _unlinked

MUTABLE = frozenset({"ordinary-instructions.md", "seat-instructions.md", "compact.md",
                     "card-summary.md", "wake-after-compact.md"})
MAX_BYTES = 64 * 1024


class PromptError(ValueError):
    """An unsafe or unreadable scoped prompt is explicitly refused."""


def filename(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*\.md", name):
        raise PromptError("Prompt name must be one lowercase Markdown filename")
    return name


def mutable(name: str) -> str:
    name = filename(name)
    if name not in MUTABLE:
        raise PromptError(f"Per-seat override refused: {name} is global-only")
    return name


def checked(path: Path) -> Path:
    try:
        return _unlinked(Path(path))
    except StoreError as exc:
        raise PromptError(str(exc)) from exc


def read_text(path: Path, *, limit: int = MAX_BYTES) -> str:
    path = checked(path)
    if path.stat().st_size > limit:
        raise PromptError("Prompt exceeds 64 KiB")
    raw = path.read_bytes()
    if len(raw) > limit or b"\0" in raw:
        raise PromptError("Prompt is oversized or contains NUL bytes")
    try:
        # Match the existing Path.read_text universal-newline prompt contract.
        return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError as exc:
        raise PromptError("Prompt must be UTF-8") from exc


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def metadata_path(directory: Path, name: str) -> Path:
    return checked(directory / "prompts" / f"{filename(name)}.json")


def metadata(directory: Path, name: str) -> dict | None:
    path = metadata_path(directory, name)
    if not path.exists():
        return None
    try:
        row = json.loads(read_text(path, limit=MAX_BYTES * 6 + 1024))
    except (OSError, ValueError) as exc:
        raise PromptError(f"Invalid prompt revision metadata: {name}") from exc
    if (not isinstance(row, dict) or row.get("version") != 1
            or type(row.get("active")) is not bool
            or not isinstance(row.get("global_text"), str)
            or len(row["global_text"].encode("utf-8")) > MAX_BYTES or "\0" in row["global_text"]
            or row.get("global_sha") != sha(row["global_text"])
            or not isinstance(row.get("body_sha"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", row["body_sha"])):
        raise PromptError(f"Invalid prompt revision metadata: {name}")
    return row


def scoped_file(directory: Path, name: str) -> Path | None:
    """Protected-name refusal precedes tombstone parsing, even when inactive."""
    name = filename(name)
    directory = checked(directory)
    path = checked(directory / "prompts" / name)
    meta = metadata_path(directory, name)
    if name not in MUTABLE:
        if path.exists() or meta.exists():
            mutable(name)  # explicit refusal, never a silent global fallback
        return None
    row = metadata(directory, name)
    if row is not None and not row["active"]:
        return None
    if path.exists():
        if not path.is_file():
            raise PromptError("Scoped prompt must be a regular file")
        if row is not None and row["body_sha"] != sha(read_text(path)):
            raise PromptError("Scoped prompt disagrees with revision metadata; publication outcome uncertain")
        return path
    if row is not None and row["active"]:
        raise PromptError("Active scoped prompt is missing")
    return None


def validate_layer(directory: Path | None) -> None:
    if directory is None:
        return
    folder = checked(directory / "prompts")
    if not folder.exists():
        return
    for path in folder.iterdir():
        checked(path)
        name = path.name[:-5] if path.name.endswith(".md.json") else path.name
        if name.endswith(".md"):
            if name not in MUTABLE:
                raise PromptError(f"Per-seat override refused: {name} is global-only")
        elif path.name not in {"audit.jsonl", "audit.jsonl.lock", "revisions"}:
            temporary = re.fullmatch(r"\.([a-z][a-z0-9-]*\.md)(?:\.json)?\.[0-9a-f]{16}\.tmp", path.name)
            if temporary is None or temporary[1] not in MUTABLE or not path.is_file():
                raise PromptError(f"Unrecognized scoped prompt file: {path.name}")
            # A retained failed publication is diagnostic only, never an active layer.


def stamp(path: Path) -> tuple:
    path = checked(path)
    try:
        info = path.stat()
        return (str(path), info.st_mtime_ns, info.st_size, info.st_ctime_ns)
    except FileNotFoundError:
        return (str(path), None)


@dataclass(frozen=True)
class Snapshot:
    directory: Path | None
    signature: tuple
    texts: dict[str, str]
    overridden: frozenset[str]


def refresh(app) -> Snapshot:
    """Check mtime at each turn; the returned snapshot stays fixed mid-turn."""
    from litetui import paths
    directory = getattr(app, "convo_dir", None)
    validate_layer(directory)
    resolved = {name: paths.prompt_file(name, convo_dir=directory) for name in sorted(MUTABLE)}
    signature = tuple((stamp(path), stamp(metadata_path(directory, name)) if directory else ())
                      for name, path in resolved.items())
    previous = getattr(app, "_prompt_snapshot", None)
    if previous is not None and previous.directory == directory and previous.signature == signature:
        return previous
    texts = {name: read_text(path) for name, path in resolved.items()}
    active = frozenset(name for name in MUTABLE
                       if directory is not None and scoped_file(directory, name) is not None)
    final = {name: paths.prompt_file(name, convo_dir=directory) for name in sorted(MUTABLE)}
    after = tuple((stamp(path), stamp(metadata_path(directory, name)) if directory else ())
                  for name, path in final.items())
    if signature != after:
        raise PromptError("Prompt publication changed during snapshot; input held unsent, retry next turn")
    for name in active:
        row = metadata(directory, name)
        if row is not None and row["body_sha"] != sha(texts[name]):
            raise PromptError("Prompt snapshot disagrees with managed revision; input held unsent")
    result = Snapshot(directory, signature, texts, active)
    app._prompt_snapshot = result
    return result


def text_for(app, name: str, *, fresh: bool = False) -> str:
    name = mutable(name)
    snapshot = getattr(app, "_prompt_snapshot", None)
    if fresh:
        previous = snapshot
        try:
            return refresh(app).texts[name]
        finally:
            app._prompt_snapshot = previous
    if snapshot is None or snapshot.directory != getattr(app, "convo_dir", None):
        snapshot = refresh(app)
    return snapshot.texts[name]


def native_context(app, *, snapshot: Snapshot | None = None) -> str:
    snapshot = snapshot or getattr(app, "_prompt_snapshot", None) or refresh(app)
    return ("[LiteTUI ordinary guidance — host supplied, not user-authored. This replaces only "
            "earlier ordinary guidance, never the immutable security, ownership, tool approval "
            "or messaging instructions. An empty seat section withdraws prior seat additions.]\n"
            + snapshot.texts["ordinary-instructions.md"]
            + "\n[Current seat additions]\n" + snapshot.texts["seat-instructions.md"]
            + "\n[End LiteTUI ordinary guidance]\n\n")
