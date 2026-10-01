"""Line-ending policy for the write/edit tools, independent of text-mode IO."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from litetui import ttyguard


def normalize(text: str, ending: str) -> str:
    """Convert incoming LF/CRLF text once; never double an existing CR."""
    return text.replace("\r\n", "\n").replace("\n", ending)


def _counts(data: bytes) -> tuple[int, int]:
    crlf = data.count(b"\r\n")
    return crlf, data.count(b"\n") - crlf


def _dominant(data: bytes) -> str | None:
    crlf, lf = _counts(data)
    if not crlf and not lf:
        return None
    return "\r\n" if crlf > lf else "\n"


def _git_ending(path: Path) -> str | None:
    """Ask Git rather than reimplement nested attributes or worktree config."""
    def git(*args: str) -> str | None:
        result = ttyguard.run(["git", *args], cwd=path.parent, timeout=2)
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        # -z avoids quoted pathnames (spaces, colons, Unicode). Success also
        # proves this is a repo, so a non-repo never inherits global Git config.
        attrs = git("check-attr", "-z", "eol", "--", path.name)
        if attrs is None:
            return None
        value = attrs.split("\0")[2]
        if value in ("lf", "crlf"):
            return "\n" if value == "lf" else "\r\n"
        autocrlf = git("config", "--get", "core.autocrlf")
        if autocrlf == "true":
            return "\r\n"
        if autocrlf == "input":
            return "\n"
        eol = git("config", "--get", "core.eol")
        if eol in ("lf", "crlf", "native"):
            return {"lf": "\n", "crlf": "\r\n", "native": os.linesep}[eol]
        if autocrlf == "false":
            return os.linesep
    except (OSError, subprocess.TimeoutExpired):
        # Git missing/unavailable: sibling convention then LF is still usable.
        pass
    return None


def write_ending(path: Path) -> str:
    """Existing ending > Git eol > checkout config > siblings > LF.

    A full overwrite may explicitly repair a mixed file: retain its dominant
    ending (LF on a tie). Empty/single-line existing files have no ending to keep.
    Sibling inference is bounded and ignores binary/non-UTF-8 files.
    """
    if path.exists():
        ending = _dominant(path.read_bytes())
        if ending is not None:
            return ending
    ending = _git_ending(path)
    if ending is not None:
        return ending
    crlf = lf = 0
    with os.scandir(path.parent) as siblings:
        for index, entry in enumerate(siblings):
            if index >= 32:
                break
            if not entry.is_file(follow_symlinks=False) or entry.name.startswith("."):
                continue
            try:
                with open(entry.path, "rb") as handle:
                    data = handle.read(65536)
                if b"\0" in data:
                    continue
                data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            c, l = _counts(data)
            crlf += c
            lf += l
    return "\r\n" if crlf > lf else "\n"
