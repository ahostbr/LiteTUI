"""Keep external Select labels as data without changing option values/styles."""
from collections.abc import Iterable
from typing import TypeVar

from rich.text import Text

Value = TypeVar("Value")


def literal_options(options: Iterable[tuple[object, Value]]) -> list[tuple[object, Value]]:
    """Rich Text bypasses markup in both Select's current label and overlay.

    Preserve existing renderables, including deliberately styled Text; only
    plain strings need a literal boundary. Values keep their identity.
    """
    return [(Text(label) if isinstance(label, str) else label, value)
            for label, value in options]
