"""Bounded history index preserves exact pages without rescanning earlier text."""
from __future__ import annotations

from typing import Any

import pytest

from litetui import history_index, history_projection
from litetui.history_index import HistoryIndex


@pytest.mark.parametrize('messages', [
    [],
    [{'role': 'system', 'content': 'hidden'}],
    [{'role': 'user', 'content': ''}],
    [{'role': 'user', 'content': 'same prompt'} for _ in range(1201)],
    [{'role': 'user', 'content': 'a' * 100_000}],
    [{'role': 'user', 'content': '🛰️雪e\u0301\n' * 5000}],
    [{'role': 'assistant', 'content': '\x00\x1b\u202e\r\n' * 5000}],
    [{'role': 'user', 'content': body} for body in ('', 'small', '雪' * 5000, '', '\n' * 1500, 'last')],
])
def test_index_returns_the_exact_canonical_partition_in_both_directions(messages: list[dict[str, Any]]) -> None:
    view = history_projection.project(messages)
    expected = list(history_projection.pages(view))
    index = HistoryIndex.build(view)
    assert index.page_count == len(expected)
    assert index.index_bytes <= history_index.MAX_INDEX_BYTES
    assert index.window(anchor='head')[:3] == expected[0]
    assert index.window(anchor='tail')[:3] == expected[-1]
    previous = None
    for start, end, fragments in expected:
        assert index.window(start) == (start, end, fragments, previous)
        previous = start
    position = expected[-1][0]
    for start, end, fragments in reversed(expected):
        actual_start, actual_end, actual_fragments, back = index.window(position)
        assert (actual_start, actual_end, actual_fragments) == (start, end, fragments)
        if back is not None:
            position = back
    assert back is None


def test_tail_window_does_not_iterate_pages_or_visit_previous_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    view = history_projection.project([{'role': 'user', 'content': f'row {n}'} for n in range(1500)])
    index = HistoryIndex.build(view)
    expected = index.window(anchor='tail')
    called = []
    original = history_projection.Block.fields

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail('Window rescanned canonical pages')

    def observed(block: history_projection.Block) -> dict[str, Any]:
        called.append(block.index)
        return original(block)

    monkeypatch.setattr(history_projection, 'pages', forbidden)
    monkeypatch.setattr(history_projection.Block, 'fields', observed)
    assert index.window(anchor='tail') == expected
    assert called == [row['block_index'] for row in expected[2]]
    assert len(called) <= history_projection.PAGE_FRAGMENTS


@pytest.mark.parametrize('position', [(0, 1), (-1, 0), (1, 0), (True, 0), (0, False), ('0', 0), (0,), 'bad'])
def test_only_indexed_page_boundaries_are_accepted(position: Any) -> None:
    index = HistoryIndex.build(history_projection.project([{'role': 'user', 'content': 'one page'}]))
    with pytest.raises(ValueError, match='boundary'):
        index.window(position)


def test_invalid_anchor_refuses() -> None:
    index = HistoryIndex.build(history_projection.project([]))
    with pytest.raises(ValueError, match='anchor'):
        index.window(anchor='middle')


def test_index_memory_bound_refuses_before_an_unbounded_index(monkeypatch: pytest.MonkeyPatch) -> None:
    view = history_projection.project([{'role': 'user', 'content': 'x' * 5000}])
    monkeypatch.setattr(history_index, 'MAX_INDEX_BYTES', 48)
    with pytest.raises(ValueError, match='index budget'):
        HistoryIndex.build(view)
