"""Extract a final short recap without ever flashing a partial tag on screen."""
from __future__ import annotations

import re

TAG = re.compile(r"<recap>(.*?)</recap>\s*$", re.S)


def split_recap(text: str, *, final: bool = False) -> tuple[str, str | None]:
    """(visible answer, recap). Incomplete trailing tag is held while streaming.

    Malformed recap markup is removed from display, without treating it as a
    valid summary. The small streaming lookbehind holds a split
    opening delimiter (even '<r') until it either completes or diverges.
    """
    match = TAG.search(text)
    if match:
        lines = [line.strip() for line in match.group(1).splitlines() if line.strip()]
        if 1 <= len(lines) <= 2 and sum(len(line.split()) for line in lines) <= 55:
            return text[:match.start()].rstrip(), " / ".join(lines)
    # A malformed completed tag, an unfinished opening tag, or even a split
    # delimiter at EOF must not leak model control markup into the answer.
    if final:
        start = text.find("<recap")
        if start >= 0:
            return text[:start].rstrip(), None
        for count in range(min(len(text), len("<recap") - 1), 0, -1):
            if text.endswith("<recap"[:count]):
                return text[:-count].rstrip(), None
        return text, None
    start = text.rfind("<recap>")
    if start >= 0 and "</recap>" not in text[start:]:
        return text[:start].rstrip(), None
    for count in range(min(len(text), len("<recap>") - 1), 0, -1):
        if text.endswith("<recap>"[:count]):
            return text[:-count], None
    return text, None
