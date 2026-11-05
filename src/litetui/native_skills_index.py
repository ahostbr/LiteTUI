"""Prepare and publish an exact skills metadata index from configured local libraries."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import math
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input, gui_rpc, paths, skills
from litetui.agent_store import _unlinked
from litetui.plugin_reload_provenance import native_skills_provenance_ok
from litetui.plugin_reload_skills import refresh_refusal

MAX_ROOTS = 16
MAX_ENTRIES = 2048
MAX_ROWS = 512
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_CACHE = 1024 * 1024
MAX_DIFF = 8192


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _encoded(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _read(path, limit, *, missing=False):
    _unlinked(path)
    try:
        before = path.stat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > limit
    ):
        raise ValueError(
            "Skills index refresh requires bounded, unlinked regular files"
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not os.path.samestat(before, opened):
            raise ValueError("Skills index input changed during inspection")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    current = _unlinked(path).stat()
    if (
        len(data) > limit
        or len(data) != before.st_size
        or not os.path.samestat(current, after)
        or any(
            s.st_nlink != 1
            or s.st_mtime_ns != before.st_mtime_ns
            or s.st_size != before.st_size
            for s in (opened, after, current)
        )
    ):
        raise ValueError("Skills index input changed during inspection")
    return data


@dataclass
class Scan:
    entries: int = 0
    size: int = 0

    def children(self, directory):
        with os.scandir(_unlinked(directory)) as entries:
            found = []
            for entry in entries:
                self.entries += 1
                if self.entries > MAX_ENTRIES:
                    raise ValueError(
                        "Configured skills libraries exceed the bounded refresh limit"
                    )
                found.append(Path(entry.path))
        return found

    def roots(self, root, patterns):
        if not isinstance(patterns, (tuple, list)) or len(patterns) > MAX_ROOTS:
            raise ValueError("Too many configured skills libraries")
        local = _unlinked(root / skills.SKILLS_DIR_NAME)
        extra = []
        for raw in patterns:
            if not isinstance(raw, str) or len(raw) > 4096:
                raise ValueError("Invalid configured skills library")
            if not raw.strip():
                continue
            path = Path(os.path.expanduser(raw.strip()))
            if (
                not path.is_absolute()
                or ".." in path.parts
                or os.name == "nt"
                and len(path.drive.removeprefix("\\\\?\\")) != 2
            ):
                raise ValueError(
                    "Reviewed refresh supports absolute local skills libraries"
                )
            parts = path.parts
            wild = [
                i for i, part in enumerate(parts) if any(ch in part for ch in "*?[")
            ]
            if not wild:
                candidates = [_unlinked(path)] if _unlinked(path).is_dir() else []
            elif len(wild) == 1 and "**" not in parts[wild[0]]:
                index = wild[0]
                parent = _unlinked(Path(*parts[:index]))
                hits = []
                for child in self.children(parent) if parent.is_dir() else ():
                    if (
                        not child.name.startswith(".") or parts[index].startswith(".")
                    ) and fnmatch.fnmatch(child.name, parts[index]):
                        target = _unlinked(child.joinpath(*parts[index + 1 :]))
                        if target.is_dir():
                            hits.append(target)
                candidates = skills._select_root(hits)
            else:
                raise ValueError(
                    "Skills library pattern exceeds the bounded wildcard scope"
                )
            for candidate in candidates:
                candidate = candidate.resolve(strict=True)
                if candidate not in extra:
                    extra.append(candidate)
        return [(local, "local"), *((d, skills._label_for(d)) for d in extra)]

    def discover(self, roots):
        found = []
        for directory, source in roots:
            if not directory.is_dir():
                continue
            for child in sorted(self.children(directory)):
                if not _unlinked(child).is_dir():
                    continue
                path = _unlinked(child / "SKILL.md")
                if not path.exists():
                    continue
                data = _read(
                    path, min(skills.MAX_SKILL_BYTES, MAX_SOURCE_BYTES - self.size)
                )
                self.size += len(data)
                # Match Path.read_text's universal newline translation.
                text = (
                    data.decode("utf-8", errors="replace")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                )
                found.append(skills._skill_from_text(path, source, text))
                if len(found) > MAX_ROWS:
                    raise ValueError("Skills index exceeds the reviewed row limit")
        return skills._ordered_skills(found)


def _rows(items):
    if not isinstance(items, list) or len(items) > MAX_ROWS:
        raise ValueError("Current skill index exceeds the reviewed row limit")
    rows = []
    seen = set()
    for item in items:
        row = skills.cache_row(item)
        if (
            not all(isinstance(v, str) and len(v) <= 4096 for v in row.values())
            or not row["name"]
            or row["name"] in seen
        ):
            raise ValueError("Skill metadata is ambiguous or exceeds the review limit")
        seen.add(row["name"])
        rows.append(row)
    if len(_encoded(rows)) > MAX_CACHE // 2:
        raise ValueError("Skills index exceeds the cache budget")
    return rows


def _current_cache(root):
    raw = _read(skills.cache_path(root), MAX_CACHE, missing=True)
    return _hash(raw) if raw is not None else None


@dataclass(frozen=True)
class Inputs:
    live_rows: list
    rows: list
    patterns: tuple
    roots: list
    previous_cache: str | None


def _snapshot(app):
    root = _unlinked(paths.data_root())
    patterns = app.settings.skill_roots
    if patterns is None:
        patterns = skills.DEFAULT_EXTRA_ROOTS
    scan = Scan()
    roots = scan.roots(root, patterns)
    found = scan.discover(roots)
    selected = [str(path.resolve()) for path, _ in roots]
    return found, Inputs(
        _rows(app.skills), _rows(found), tuple(patterns), selected, _current_cache(root)
    )


def _delta(old, new, count, cache):
    others = {r["name"]: r for r in new}
    lines = [f"{count} indexed skills", cache]
    for row in old:
        if others.get(row["name"]) != row:
            lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    text = "\n".join(lines)
    if len(text.encode()) > MAX_DIFF:
        raise ValueError("Skills index changes exceed the complete review limit")
    return text


@dataclass
class Review:
    bridge: object
    registry: object
    live: list
    token: str
    candidate: list
    inputs: Inputs
    document: bytes
    before: str
    after: str
    before_hash: str
    after_hash: str
    apply_id: str | None = None
    cache_hash: str = ""


def registered(app):
    from litetui.plugins import skills_plugin

    entry = app.plugins.commands.get("/skills")
    return (
        entry is not None
        and entry.owner == "skills"
        and entry.handler is skills_plugin._cmd_skills
        and "/skills" in entry.tokens
    )


def _guard(app, cmd, bridge, activity):
    if background_input.bridge(app) is not bridge:
        raise ValueError("Skills refresh belongs to a different Work epoch")
    bridge.check()
    gui_rpc._idle(app)
    if not registered(app):
        raise ValueError("The built-in Skills command must be registered")
    refusal = refresh_refusal(app, lambda: activity(app, cmd))
    if refusal:
        raise ValueError(f"Skills refresh {refusal.status}: {refusal.reason}")
    if not native_skills_provenance_ok(app):
        raise ValueError(
            "Skills index source changed or is unavailable; restart required"
        )


def _verify_cache(review):
    raw = _read(skills.cache_path(paths.data_root()), MAX_CACHE)
    if raw != review.document:
        raise ValueError("Published skills cache differs from the reviewed bytes")
    data = json.loads(raw)
    if (
        data["count"] != len(review.inputs.rows)
        or data["skills"] != review.inputs.rows
        or data["roots"] != review.inputs.roots
        or not math.isfinite(data["generated"])
    ):
        raise ValueError("Skills cache metadata differs from the reviewed candidate")
    return _hash(_encoded([data["roots"], data["skills"]]))


def dispatch(app, cmd, bridge, common, activity):
    action = cmd["type"].rsplit("_", 1)[1]
    reply = {**common, "name": "skills"}
    if action == "review":
        app._native_plugin_reload = None
        _guard(app, cmd, bridge, activity)
        candidate, inputs = _snapshot(app)
        current, after = inputs.live_rows, inputs.rows
        document = skills.cache_document(inputs.roots, after)
        if len(document) > MAX_CACHE:
            raise ValueError("Staged skills cache exceeds the byte limit")
        review = Review(
            bridge,
            app.plugins,
            app.skills,
            secrets.token_hex(32),
            candidate,
            inputs,
            document,
            _delta(
                current,
                after,
                len(current),
                "Current cache " + (inputs.previous_cache or "missing"),
            ),
            _delta(
                after,
                current,
                len(after),
                "Replace skills/index.json and install this index in the current session.",
            ),
            _hash(_encoded([current, inputs.previous_cache])),
            _hash(_encoded([inputs.roots, after])),
            cache_hash=_hash(document),
        )
        app._native_plugin_reload = review
        return {
            **reply,
            "kind": "skills_review",
            "token": review.token,
            "before": review.before,
            "after": review.after,
            "before_spec": review.before_hash,
            "after_spec": review.after_hash,
            "cache_sha256": review.cache_hash,
        }
    review = getattr(app, "_native_plugin_reload", None)
    if (
        not isinstance(review, Review)
        or review.bridge is not bridge
        or review.registry is not app.plugins
        or cmd["token"] != review.token
        or not registered(app)
    ):
        raise ValueError("Review this exact skills index again")
    if action == "apply" and review.apply_id is None:
        app._native_plugin_reload = None

        def guard():
            _guard(app, cmd, bridge, activity)
            if (
                app.plugins is not review.registry
                or app.skills is not review.live
                or _snapshot(app)[1] != review.inputs
                or _rows(review.candidate) != review.inputs.rows
            ):
                raise ValueError("Skills index or cache changed since review")

        guard()
        root = paths.data_root()
        lock = _unlinked(Path(str(skills.cache_path(root)) + ".lock"))
        if lock.exists() and (not lock.is_file() or lock.stat().st_nlink != 1):
            raise ValueError("Skills cache lock has an unexpected owner")

        def install():
            # Keep the coordinated cache writer lease until both halves are installed.
            if _verify_cache(review) != review.after_hash:
                raise ValueError(
                    "Published skills cache differs from the reviewed candidate"
                )
            _guard(app, cmd, bridge, activity)
            if (
                app.plugins is not review.registry
                or app.skills is not review.live
                or _rows(app.skills) != review.inputs.live_rows
            ):
                raise ValueError(
                    "Live skills index changed while publishing the cache; review again"
                )
            app.skills = review.candidate
            app.skills_cached_at = 0.0

        path = skills.write_cache_document(
            root, review.document, before_replace=guard, after_replace=install
        )
        if path != skills.cache_path(root) or app.skills is not review.candidate:
            raise ValueError(
                "Skills cache publication could not be verified; review again"
            )
        review.apply_id = cmd["id"]
        app._native_plugin_reload = review
    expected_id = cmd["id"] if action == "apply" else cmd["apply_id"]
    if review.apply_id != expected_id or app.skills is not review.candidate:
        raise ValueError("Skills index application no longer owns this runtime index")
    result = {
        **reply,
        "kind": "skills_applied",
        "token": review.token,
        "apply_id": review.apply_id,
        "after_spec": review.after_hash,
        "cache_sha256": review.cache_hash,
    }
    if action == "readback":
        patterns = (
            app.settings.skill_roots
            if app.settings.skill_roots is not None
            else skills.DEFAULT_EXTRA_ROOTS
        )
        roots = [
            str(path.resolve()) for path, _ in Scan().roots(paths.data_root(), patterns)
        ]
        live = _hash(_encoded([roots, _rows(app.skills)]))
        cache = _verify_cache(review)
        if (
            live != review.after_hash
            or cache != review.after_hash
            or tuple(patterns) != review.inputs.patterns
        ):
            raise ValueError("Live skills index or cache changed after publication")
        result.update(kind="skills_verified", live_index=live, cache_index=cache)
    return result
