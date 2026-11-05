"""Bounded current-agent memory topic names under the owned memories directory."""

from __future__ import annotations

import os
import stat
import unicodedata

from litetui.agent_store import _unlinked, name_key, valid_name
from litetui.gui_rpc import _owned_memory_path

PAGE = "gui.context.native_topic_page"
INDEX = "topics"
MAX_PATH_BYTES = 512
MAX_DEPTH = 16
MAX_ENTRIES = 2048
MAX_TOPICS = 256


def valid_topic(name):
    if (
        type(name) is not str
        or len(name) > MAX_PATH_BYTES
        or len(name.encode("utf-8")) > MAX_PATH_BYTES
    ):
        raise ValueError("Invalid memory topic name")
    parts = name.split("/")
    if (
        len(parts) < 2
        or len(parts) > MAX_DEPTH + 1
        or parts[0] != "memories"
        or not parts[-1].endswith(".md")
    ):
        raise ValueError("Choose a Markdown topic inside memories/")
    for part in parts[1:]:
        valid_name(part)
    return name


def _root(app):
    if getattr(app, "_owner_memory_dir", None) is not None:
        raise ValueError(
            "Topic access requires the current agent's canonical memory root"
        )
    return _unlinked(app._agent_session.memory_root)


def _exact_spelling(parent, component, remaining):
    # Windows accepts case aliases. Only the spelling from the current directory is a topic path.
    exact = False
    with os.scandir(_unlinked(parent)) as entries:
        for entry in entries:
            if entry.name == component:
                exact = True
            remaining -= 1
            if remaining < 0:
                raise OverflowError
            if (
                entry.name != component
                and unicodedata.normalize("NFC", entry.name).casefold()
                == component.casefold()
            ):
                raise ValueError("Use the exact spelling of the existing topic path")
    if not exact and (parent / component).exists():
        raise ValueError("Use the exact spelling of the existing topic path")
    return remaining


def checked_path(app, name):
    target = _owned_memory_path(app._agent_session, valid_topic(name))
    parent = _root(app)
    remaining = MAX_ENTRIES
    try:
        for component in name.split("/"):
            if not parent.exists():
                break
            remaining = _exact_spelling(parent, component, remaining)
            parent = parent / component
    except OverflowError as exc:
        raise ValueError("Topic path exceeds the directory inspection limit") from exc
    return _unlinked(target)


def catalog(app):
    root = _root(app)
    folder = _unlinked(root / "memories")
    try:
        remaining = _exact_spelling(root, "memories", MAX_ENTRIES)
    except OverflowError:
        return "oversize", None
    if not folder.exists():
        return "content", b""
    topics = []

    def walk(directory, depth):
        nonlocal remaining
        if depth > MAX_DEPTH:
            raise OverflowError
        directory = _unlinked(directory)
        before = directory.stat()
        seen = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                remaining -= 1
                if remaining < 0:
                    raise OverflowError
                path = _unlinked(directory / entry.name)
                # Windows DirEntry.stat() can omit the hard-link count.
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) and not entry.name.endswith(".md"):
                    continue
                folded = name_key(entry.name)
                if folded in seen:
                    raise ValueError("Memory topics contain ambiguous names")
                seen.add(folded)
                if stat.S_ISDIR(info.st_mode):
                    walk(path, depth + 1)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Memory topic must be a regular unlinked file")
                name = valid_topic(path.relative_to(root).as_posix())
                _owned_memory_path(app._agent_session, name)
                if len(topics) >= MAX_TOPICS:
                    raise OverflowError
                topics.append(name)
        after = _unlinked(directory).stat()
        if (before.st_ino, before.st_dev, before.st_mtime_ns) != (
            after.st_ino,
            after.st_dev,
            after.st_mtime_ns,
        ):
            raise ValueError("Memory topic directory changed while listing")

    try:
        walk(folder, 1)
    except OverflowError:
        return "oversize", None
    return "content", "\n".join(sorted(topics)).encode("utf-8")
