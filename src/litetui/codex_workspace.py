"""The host execution workspace, independent of package installation paths."""

from pathlib import Path


def workspace(app=None):
    captured = getattr(app, "_hook_workspace", None)
    return Path(captured).resolve() if captured is not None else Path.cwd().resolve()
