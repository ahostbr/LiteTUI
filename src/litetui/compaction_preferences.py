"""Pure bounded preference vocabulary shared by reviewed compaction and its editor."""
from __future__ import annotations

from typing import get_args

from litetui.settings import ThinkingLevel

FIELDS = ("compact_keep_recent", "compact_max_tokens", "compact_max_tool_iters",
          "compact_thinking_level", "wake_after_compact", "clear_screen_after_compact")
BOUNDS = {"compact_keep_recent": (0, 1000000), "compact_max_tokens": (256, 2147483647),
          "compact_max_tool_iters": (1, 512)}
CHOICES = get_args(ThinkingLevel)


def checked(key, value):
    if key in BOUNDS:
        low, high = BOUNDS[key]
        valid = type(value) is int and low <= value <= high
    elif key == "compact_thinking_level":
        valid = isinstance(value, str) and value in CHOICES
    elif key in ("wake_after_compact", "clear_screen_after_compact"):
        valid = type(value) is bool
    else:
        raise ValueError("Unsupported compaction preference: " + str(key))
    if not valid:
        raise ValueError("Unsupported compaction configuration: " + key)
    return value
