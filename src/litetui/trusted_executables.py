"""Resolve installed developer tools without treating an executable name as trust.

This module performs location lookups only; it never launches an executable.
The policy module itself remains a pure classifier with no process API imports.
"""
from __future__ import annotations

import shutil
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
