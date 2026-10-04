"""Resolve installed developer tools without treating an executable name as trust.

This module performs location lookups only; it never launches an executable.
The policy module itself remains a pure classifier with no process API imports.
"""
from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path


def is_installed_tool(path: Path) -> bool:
    """Whether this exact resolved path is in the running venv or on PATH."""
    path = path.resolve()
    runtime = Path(sys.prefix).resolve()
    if path == Path(sys.executable).resolve() or path.is_relative_to(runtime):
        return True
    installed = shutil.which(path.name)
    return bool(installed and path == Path(installed).resolve())


def _unlinked_absolute(raw: object) -> Path | None:
    """No expansion, relative paths, dot traversal, symlinks or reparse ancestors.

    Check on every classification, so retargeting a configured link cannot carry
    a previous approval to another executable. This is not a filesystem sandbox.
    """
    if not isinstance(raw, str) or not raw or raw != raw.strip():
        return None
    path = Path(raw)
    if not path.is_absolute() or ".." in path.parts:
        return None
    try:
        for part in (path, *path.parents):
            info = part.lstat()
            if (stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                return None
        if not path.is_file() or path.resolve(strict=True) != path:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return path


def is_configured_interpreter(raw: str, configured: object) -> bool:
    """An explicit human setting names one exact executable, never its directory.

    A malformed whole setting or any invalid entry grants nothing. Case/separator
    normalization follows the host Path implementation; no PATH/name expansion.
    Empty/omitted configuration preserves the installed-tool baseline above.
    """
    if not isinstance(configured, (list, tuple)) or not configured:
        return False
    paths = [_unlinked_absolute(item) for item in configured]
    if any(path is None for path in paths):
        return False
    candidate = _unlinked_absolute(raw)
    return candidate is not None and any(candidate == path for path in paths)
