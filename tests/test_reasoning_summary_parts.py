"""T676 — Codex reasoning summaries arrive in PARTS, and the parts must survive.

Measured against one real gpt-5.6-sol stream (probe, 2026-09-11): each summary
part is `part.added -> summary_text.delta* -> summary_text.done -> part.done`
and its text is a bold markdown heading with no trailing newline. Forwarding
only the deltas glues the headings together — `**A****B****C**` on one line,
which is what Ryan photographed.

The fixture below is that capture's shape, trimmed to the events the transport
reads.
"""

import json

import httpx
import pytest
from test_oauth_transport import auth_file

from litetui import model_transport as mt
from litetui.widgets import reasoning_text

COMPLETED = {
    "type": "response.completed",
    "response": {"output": [], "usage": {"input_tokens": 1, "output_tokens": 1}},
}


def sse(*events) -> str:
    """The events, then the completion the transport requires before it yields."""
    return "".join(
        "data: " + json.dumps(e) + "\n\n" for e in (*events, COMPLETED)
    )


def part(index: int, text: str) -> list:
    """One summary part, exactly as the live stream sends it."""
    return [
        {
            "type": "response.reasoning_summary_part.added",
            "output_index": 0,
            "summary_index": index,
            "part": {"type": "summary_text", "text": ""},
        },
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "summary_index": index,
            "delta": text,
        },
        {
            "type": "response.reasoning_summary_text.done",
            "output_index": 0,
            "summary_index": index,
            "text": text,
        },
        {
            "type": "response.reasoning_summary_part.done",
            "output_index": 0,
            "summary_index": index,
            "part": {"type": "summary_text", "text": text},
        },
    ]


async def reasoning_of(tmp_path, body: str) -> str:
    transport = mt.OAuthTransport(
        "codex",
        credential_path=auth_file(tmp_path),
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body)),
    )
    stream = await transport.create(model="gpt-test", messages=[], stream=True)
    return "".join([c.choices[0].delta.reasoning_content or "" async for c in stream])


@pytest.mark.asyncio
async def test_two_summary_parts_are_separated(tmp_path):
    text = await reasoning_of(
        tmp_path,
        sse(*part(0, "**Planning the tests**"), *part(1, "**Implementing pasteText**")),
    )
    # The defect, stated as the thing that must not be true:
    assert "****" not in text
    assert text == "**Planning the tests**\n\n**Implementing pasteText**"


@pytest.mark.asyncio
async def test_one_part_gains_no_leading_separator(tmp_path):
    """The control. A separator BEFORE the first part would indent the block."""
    text = await reasoning_of(tmp_path, sse(*part(0, "**Only one**")))
    assert text == "**Only one**"


@pytest.mark.asyncio
async def test_a_second_reasoning_item_still_separates(tmp_path):
    """summary_index RESTARTS at 0 in each reasoning item (one per tool round).

    A `summary_index > 0` test would pass the arm above and still fuse the
    first heading of every later item, which is the common case in a real turn.
    """
    second = part(0, "**After the tool call**")
    for e in second:
        e["output_index"] = 2
    text = await reasoning_of(tmp_path, sse(*part(0, "**Before**"), *second))
    assert text == "**Before**\n\n**After the tool call**"


def test_the_block_renders_bold_rather_than_asterisks():
    rendered = reasoning_text("**Planning the tests**\n\nbody text")
    assert "*" not in rendered.plain
    assert rendered.plain == "Planning the tests\n\nbody text"
    assert any("bold" in str(span.style) for span in rendered.spans)


def test_unclosed_bold_and_bare_markup_survive():
    """Reasoning text is arbitrary: a half-streamed heading must not vanish, and
    a stray Rich tag must not be interpreted."""
    assert reasoning_text("**Still typin").plain == "**Still typin"
    assert reasoning_text("see [red] and [/]").plain == "see [red] and [/]"
