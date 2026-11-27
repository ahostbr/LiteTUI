"""Run Git for Windows' real git binary, never the wrapper a plain ``git`` resolves to (T1397-C).

A plain ``git`` on Windows resolves to ``<git root>\\cmd\\git.exe`` (or ``bin\\git.exe``), a small
launcher that starts ``<git root>\\mingw64\\bin\\git.exe``. Each launcher start leaks about one kernel
Token object (~2.9 KB paged pool); the real binary leaks none (measured 2026-11-26,
``C:\\Projects\\.scratch\\T1397\\spawn-test5-200.txt``). So every git process we start runs the real
binary, with what the launcher would have set: ``usr\\bin`` (sh, sed... for hooks and credential
helpers) and ``mingw64\\bin`` first on that child's PATH, and ``MSYSTEM`` when unset.

The install is found from the ``git`` on PATH, never a fixed path. Anywhere the layout is absent
(Linux, macOS, a Git without ``mingw64``) this falls back to plain ``git`` and leaves env untouched.

    subprocess.run(git_cmd("status"), env=git_env(), ...)
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _resolve() -> tuple[str, tuple[str, ...]]:
    """(git executable, directories to prepend to the child's PATH)."""
    if os.name != "nt":
        return "git", ()
    found = shutil.which("git")
    if not found:
        return "git", ()
    exe = Path(found)
    parent = exe.parent
    if parent.name.lower() == "bin" and parent.parent.name.lower() == "mingw64":
        root = parent.parent.parent          # already <root>\mingw64\bin\git.exe
    elif parent.name.lower() in ("cmd", "bin"):
        root = parent.parent                 # <root>\cmd\git.exe or <root>\bin\git.exe
    else:
        return "git", ()
    real = root / "mingw64" / "bin" / "git.exe"
    if not real.is_file():
        return "git", ()
    return str(real), (str(root / "usr" / "bin"), str(root / "mingw64" / "bin"))


def git_exe() -> str:
    """The git executable to start: the real binary when found, else ``"git"``."""
    return _resolve()[0]


def git_cmd(*args: str) -> list[str]:
    """``[git_exe(), *args]``, for subprocess argv lists."""
    return [git_exe(), *args]


def git_env(env: dict[str, str] | None = None) -> dict[str, str] | None:
    """The environment for a git child: ``env`` (default: this process's) with the PATH prefix.

    Returns ``env`` unchanged (``None`` = inherit) when the real binary was not found.
    """
    prefix = _resolve()[1]
    if not prefix:
        return env
    out = dict(os.environ if env is None else env)
    key = next((k for k in out if k.upper() == "PATH"), "PATH")
    out[key] = os.pathsep.join([*prefix, out.get(key, "")])
    out.setdefault("MSYSTEM", "MINGW64")
    return out
