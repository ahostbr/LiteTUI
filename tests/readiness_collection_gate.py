"""Opt-in runner plugin: every explicitly selected Python file needs an item.

This detects empty/ignored selected files, not lost duplicate definitions or
whether collected tests actually execute assertions. Directory discovery is
outside its scope. Activated by run_all via -p, not global conftest behavior.
"""
from pathlib import Path
import pytest


def uncollected_files(arguments, items, invocation_dir):
    def canonical(value):
        path = Path(value)
        if not path.is_absolute():
            path = Path(invocation_dir) / path
        return path.resolve()

    expected = {canonical(str(arg).split("::", 1)[0]) for arg in arguments
                if str(arg).split("::", 1)[0].endswith(".py")}
    collected = {canonical(item.path) for item in items}
    return sorted(str(path) for path in expected - collected)


def pytest_collection_finish(session):
    missing = uncollected_files(session.config.args, session.items,
                                session.config.invocation_params.dir)
    if missing:
        raise pytest.UsageError("Selected files have zero collected items: " + ", ".join(missing))
