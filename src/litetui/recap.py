"""Project assistant output to the pane and RPC without model-only recap tags."""
from __future__ import annotations

OPEN = "<recap>"
CLOSE = "</recap>"


def _summary(body: str) -> str | None:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if 1 <= len(lines) <= 2 and sum(len(line.split()) for line in lines) <= 55:
        return " / ".join(lines)
    return None


def _opening(text: str, offset: int) -> int | None:
    start = text.find(OPEN, offset)
    if start >= 0:
        return start
    for count in range(min(len(text) - offset, len(OPEN) - 1), 0, -1):
        if text.endswith(OPEN[:count]):
            return len(text) - count
    return None


def split_recap(text: str, *, final: bool = False) -> tuple[str, str | None]:
    """Return visible answer and the final valid recap (if last in answer).

    Each complete pair is removed; a partial opening or unclosed pair is held
    rather than shown. The caller holds trailing whitespace while streaming so
    a later opening can consume it without retracting an earlier RPC delta.
    """
    visible = ""
    offset = 0
    last: str | None = None
    last_end = -1
    while (start := _opening(text, offset)) is not None:
        visible += text[offset:start]
        if not text.startswith(OPEN, start):
            return visible.rstrip(), None
        end = text.find(CLOSE, start + len(OPEN))
        if end < 0:
            return visible.rstrip(), None
        last = _summary(text[start + len(OPEN):end])
        last_end = end + len(CLOSE)
        offset = last_end
    visible += text[offset:]
    if last_end < 0:
        return visible, None
    # Only the last tag is eligible, and only if no prose follows it.
    recap = last if final and not text[last_end:].strip() else None
    return visible.rstrip() if not text[last_end:].strip() else visible, recap


class RecapStream:
    """One append-only projection shared by the display sink and RPC emitter."""

    def __init__(self) -> None:
        self._raw = ""
        self.visible = ""
        self.recap: str | None = None

    @property
    def raw(self) -> str:
        return self._raw

    def feed(self, chunk: str) -> str:
        self._raw += chunk
        shown, _ = split_recap(self._raw)
        shown = shown.rstrip()  # trailing whitespace might precede a split tag
        if not shown.startswith(self.visible):
            raise ValueError("recap projection cannot retract already emitted text")
        delta = shown[len(self.visible):]
        self.visible = shown
        return delta

    def finish(self) -> str:
        shown, self.recap = split_recap(self._raw, final=True)
        if not shown.startswith(self.visible):
            raise ValueError("recap finalization cannot retract already emitted text")
        delta = shown[len(self.visible):]
        self.visible = shown
        return delta
