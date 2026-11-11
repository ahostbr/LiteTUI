"""Bounded, read-only preparation of an exact discovered skill for attended input.

File digests describe the fresh read, not a file revision captured by the catalog.
No legacy name/folder loader, handler, prompt admission or provider runs here.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from litetui import command_surface, skills
from litetui.file_observations import stable_read

MAX_BYTES = 65536
MAX_RESPONSE_BYTES = 1048576


def _bytes(value, label):
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    try:
        result = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8") from exc
    if len(result) > MAX_BYTES:
        raise ValueError(f"{label} exceeds the 65536-byte preparation budget")
    return result


def _identity(info, *, birthtime=False):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_birthtime_ns if birthtime else info.st_ctime_ns)


def _read(path):
    """Bound regular-file reads and compare the selected path to the open handle."""
    try:
        target = path.resolve(strict=True)
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise ValueError("Skill source must be a regular file of at most 65536 bytes")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stable_read(before, opened, opened, before, stamp=_identity)
                    or not stat.S_ISREG(opened.st_mode)):
                raise ValueError("Skill source changed before reading; prepare it again")
            data = stream.read(MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (len(data) > MAX_BYTES or len(data) != before.st_size
                or not stable_read(before, opened, after, path.stat(), stamp=_identity)
                or path.resolve(strict=True) != target):
            raise ValueError("Skill source changed while reading; prepare it again")
        return data, before, str(target)
    except OSError as exc:
        raise ValueError("Selected skill source could not be read") from exc


def _wire_int(value, *, signed=False):
    minimum, maximum = (-(2 ** 63), 2 ** 63 - 1) if signed else (0, 2 ** 64 - 1)
    return value if minimum <= value <= maximum else None


def prepare(app, cmd):
    """Return a deterministic exact prompt, without changing execution or disk."""
    current = command_surface._selection(app, cmd)
    if cmd.get("backend") != current["backend"] or cmd.get("model") != current["model"]:
        raise ValueError("Skill preparation belongs to another backend or model; refresh")
    arguments = cmd.get("arguments")
    argument_bytes = _bytes(arguments, "Skill arguments")
    selected = next((row for row in current["entries"] if row["id"] == cmd.get("row_id")), None)
    if selected is None or selected["kind"] != "skill" or selected["native_action"] != "skill_prepare":
        raise ValueError("Skill preparation requires an exact discovered skill row")
    matches = [skill for skill in app.skills if command_surface._skill_row(skill)["id"] == selected["id"]]
    if len(matches) != 1:
        raise ValueError("Selected skill object is missing or changed; refresh")
    skill = matches[0]
    path = Path(skill.path)
    data, info, resolved = _read(path)
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Selected skill source is not valid UTF-8") from exc
    header = f"# Skill: {skill.name}\n\n"
    directory = str(path.parent)
    # Bound canonical substitution before allocating its possibly expanded text.
    expanded_bytes = len(data)
    for placeholder in skills.SKILL_DIR_PLACEHOLDERS:
        expanded_bytes += text.count(placeholder) * (len(directory.encode("utf-8")) - len(placeholder.encode("utf-8")))
    wrapper_bytes = len((header + "Base directory for this skill: " + directory + "\n\n").encode("utf-8"))
    if expanded_bytes + wrapper_bytes + (len(argument_bytes) + 7 if arguments else 0) > MAX_BYTES:
        raise ValueError("Prepared skill prompt exceeds the 65536-byte preparation budget")
    body = header + skills.bind_skill_dir(text, path.parent)
    prompt = body + ("\n\n---\n\n" + arguments if arguments else "")
    prompt_bytes = _bytes(prompt, "Prepared skill prompt")
    after = command_surface._selection(app, cmd)
    if after != current or not any(candidate is skill for candidate in app.skills):
        raise ValueError("Skill catalog or owned context changed while preparing; refresh")
    again, again_info, again_resolved = _read(path)
    if again != data or _identity(again_info) != _identity(info) or again_resolved != resolved:
        raise ValueError("Skill source changed while preparing; prepare it again")
    # Re-check after the second read too; a file read may not publish a stale owner.
    if command_surface._selection(app, cmd) != current:
        raise ValueError("Skill catalog or owned context changed while preparing; refresh")
    result = {"version": 1, "kind": "skill_prompt", "session_id": current["session_id"],
            "agent_id": current["agent_id"], "backend": current["backend"], "model": current["model"],
            "snapshot": current["snapshot"], "row_id": selected["id"], "arguments": arguments,
            "skill": {"name": skill.name, "source": skill.source, "path": str(path)},
            "file": {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                     "mtime_ns": _wire_int(info.st_mtime_ns, signed=True),
                     "device": _wire_int(info.st_dev), "inode": _wire_int(info.st_ino),
                     "resolved_path": resolved},
            "body": body, "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest()}
    # Match the actual rpc._respond default ASCII escaping and envelope.
    response = {"type": "response", "ok": True}
    if cmd.get("id") is not None:
        response["id"] = cmd["id"]
    response["result"] = result
    if len(json.dumps(response, default=str).encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("Prepared skill response exceeds the 1 MiB wire budget; current session is unchanged")
    return result
