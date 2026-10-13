"""Read launch/store compatibility from saved prompt content, never settings.

A resume is not a migration. Refuse before adopting it instead of replacing a
snapshot or a recorded native prompt with a different store's contents.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

from litetui.owner_mode import OwnerModeError

_HEADER = re.compile(r"^## Your conversation store(?P<owner> \(owner memory folder\))?\s*$", re.MULTILINE)
_ADDRESS = re.compile(r"Your conversation id \(storage only\) is `[^`]+`.*?\n    ([^\r\n]+)\r?\n", re.DOTALL)
_OWNER_HEADINGS = {"SOUL.md", "NOW.md", "DOMAIN.md"}
_HOME_HEADINGS = {"memory.md", "soul.md", "handoff.md"}


def _identity(prompt: str) -> Path | None:
    from litetui.appsvc import STORE_HEADER

    address, _, snapshot = prompt.partition(STORE_HEADER)
    identities: list[Path | None] = []
    headers = list(_HEADER.finditer(address))
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(address)
        if header.group("owner"):
            match = _ADDRESS.search(address[header.end():end])
            if match is None or not Path(match[1].strip()).is_absolute():
                raise ValueError("saved owner address is missing or unreadable")
            identities.append(Path(match[1].strip()))
        else:
            identities.append(None)
    keys = {_key(value) for value in identities}
    if len(keys) > 1:
        raise ValueError("saved prompt names conflicting stores")
    identity = identities[0] if identities else None
    # store_block writes a notice, then the first file heading, then raw file
    # bodies. Only that first heading is unambiguously host-authored framing:
    # later identical-looking lines can be legal Markdown in those bodies.
    first = re.search(r"^### ([^\r\n]+) \(current contents\)$", snapshot, re.MULTILINE)
    if first is not None:
        heading = first[1]
        if (heading in _OWNER_HEADINGS and identity is None
                or heading in _HOME_HEADINGS and identity is not None):
            raise ValueError("saved address and snapshot identify different stores")
    return identity


def _key(path: Path | None) -> str | None:
    return os.path.normcase(os.path.abspath(path)) if path is not None else None


def require_store_match(prompt: str, owner_dir, *, agent_name: str, convo_id: str) -> None:
    """Raise one actionable refusal unless the saved store matches this launch."""
    expected = Path(owner_dir) if owner_dir is not None else None
    command = ["litetui"]
    try:
        saved = _identity(prompt)
    except ValueError as exc:
        reason = str(exc)
        if expected is not None:
            command += ["--owner", agent_name, "--memory", str(expected)]
        else:
            command += ["--agent", agent_name]
        remedy = "start a new conversation (omit --convo) with"
    else:
        if _key(saved) == _key(expected):
            return
        reason = "saved conversation and launch use different memory stores"
        if saved is None:
            command += ["--agent", agent_name]
            remedy = "resume without --owner and --memory using"
        else:
            command += ["--owner", agent_name, "--memory", str(saved)]
            remedy = "resume with the saved owner flags using"
        command += ["--convo", convo_id]
    rendered = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    raise OwnerModeError(f"Store mismatch: {reason}; {remedy}: {rendered}")
