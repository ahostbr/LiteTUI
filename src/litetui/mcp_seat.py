"""Invocation-only project MCP selection; never persisted to shared config."""
from __future__ import annotations


def server_selection(value: str | None) -> frozenset[str] | None:
    """None means all; an empty set means none; other sets are exact names."""
    if value is None or value.strip() == "all":
        return None
    if value.strip() == "none":
        return frozenset()
    names = [name.strip() for name in value.split(",")]
    if not names or any(not name or name in {"all", "none"} for name in names):
        raise ValueError("MCP servers must be all, none, or comma-separated server names")
    return frozenset(names)


def selected(name: str, servers: frozenset[str] | None) -> bool:
    return servers is None or name in servers


def warn_unknown(servers: frozenset[str] | None, configured) -> None:
    """A misspelled invocation subset must not silently look like an empty project."""
    import warnings
    if servers is not None:
        unknown = servers - set(configured)
        if unknown:
            warnings.warn("Unknown seat MCP server(s): " + ", ".join(sorted(unknown)),
                          RuntimeWarning, stacklevel=2)
