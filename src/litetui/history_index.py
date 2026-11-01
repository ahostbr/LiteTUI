"""Compact page boundaries for inert history, with no retained page-text cache."""
from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Any

from litetui import history_projection as projection

# Packed offsets are independently bounded; the immutable view already has its
# own context/projection limits. No strings are copied into this index.
MAX_INDEX_BYTES = 1024 * 1024
Position = tuple[int, int]
Window = tuple[Position, Position, list[dict[str, Any]], Position | None]


@dataclass(frozen=True)
class HistoryIndex:
    view: projection.Projection
    _boundaries: array[int]
    _block_bytes: array[int]

    @classmethod
    def build(cls, view: projection.Projection) -> HistoryIndex:
        boundaries = array('Q', (0, 0, 0))
        sizes = array('Q')

        def check(extra: int = 0) -> None:
            if (len(boundaries) + len(sizes) + extra) * boundaries.itemsize > MAX_INDEX_BYTES:
                raise ValueError('Saved history exceeds its packed index budget')

        check(len(view.blocks))
        sizes.extend(len(block.content.encode('utf-8')) for block in view.blocks)
        for start, end, fragments in projection.pages(view):
            prior = (boundaries[-3], boundaries[-2])
            if start != prior:
                raise ValueError('History partition changed while building its index')
            end_char = 0
            if end[1]:
                if not fragments or fragments[-1]['block_index'] != end[0]:
                    raise ValueError('History partition has an invalid ending block')
                last = fragments[-1]
                if last['part']['utf8_end'] != end[1]:
                    raise ValueError('History partition has an invalid ending offset')
                # A partial ending block either continues the first block or
                # begins within this page. Character and byte offsets differ.
                end_char = (boundaries[-1] if start[0] == end[0] else 0) + len(last['text'])
            check(3)
            boundaries.extend((end[0], end[1], end_char))
        if len(boundaries) < 6 or tuple(boundaries[-3:]) != (len(view.blocks), 0, 0):
            raise ValueError('History partition is incomplete')
        return cls(view, boundaries, sizes)

    @property
    def page_count(self) -> int:
        return len(self._boundaries) // 3 - 1

    @property
    def index_bytes(self) -> int:
        return len(self._boundaries) * self._boundaries.itemsize + len(self._block_bytes) * self._block_bytes.itemsize

    def _entry(self, index: int) -> tuple[int, int, int]:
        offset = index * 3
        return self._boundaries[offset], self._boundaries[offset + 1], self._boundaries[offset + 2]

    def _find(self, position: Position) -> int:
        if (not isinstance(position, tuple) or len(position) != 2
                or any(type(value) is not int or value < 0 for value in position)):
            raise ValueError('Invalid saved-history page boundary')
        left, right = 0, self.page_count
        while left < right:
            middle = (left + right) // 2
            candidate = self._entry(middle)[:2]
            if candidate < position:
                left = middle + 1
            else:
                right = middle
        if left >= self.page_count or self._entry(left)[:2] != position:
            raise ValueError('Unknown saved-history page boundary')
        return left

    def window(self, position: Position | None = None, *, anchor: str = 'head') -> Window:
        if anchor not in ('head', 'tail'):
            raise ValueError('History anchor must be head or tail')
        index = self._find(position) if position is not None else (0 if anchor == 'head' else self.page_count - 1)
        first, last = self._entry(index), self._entry(index + 1)
        start, end = first[:2], last[:2]
        fragments = []
        stop = last[0] + (1 if last[1] else 0)
        for number in range(first[0], stop):
            block = self.view.blocks[number]
            char_start, byte_start = (first[2], first[1]) if number == first[0] else (0, 0)
            char_end, byte_end = (last[2], last[1]) if number == last[0] else (len(block.content), self._block_bytes[number])
            text = block.content[char_start:char_end]
            data = text.encode('utf-8')
            if len(data) != byte_end - byte_start:
                raise ValueError('Indexed history character and byte boundaries disagree')
            fragments.append({**block.fields(), 'part': {
                'utf8_start': byte_start, 'utf8_end': byte_end,
                'utf8_total': self._block_bytes[number], 'sha256': projection.digest(data),
            }, 'text': text})
        previous = self._entry(index - 1)[:2] if index else None
        return start, end, fragments, previous
