"""T0642-C - the footer's bottom-right corner names the running version.

The version is `litetui.version.__version__`, the one copy. The footer must
follow it with no second literal, sit at the right edge of the lower row, and be
the first thing to go when the row is too narrow, never clipped into "v0.2" and
never pushing out the status line or the meters.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from litetui import footer_telemetry, widgets  # noqa: E402
from litetui.version import __version__  # noqa: E402

ROWS = 34


def configure(a):
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.convo_id = "c1"
    return a


def footer_rows(a) -> list[str]:
    """The two footer rows exactly as composited to the screen."""
    footer = a.query_one(widgets.ContextFooter)
    strips = a.screen._compositor.render_strips()
    top = footer.region.y
    return ["".join(seg.text for seg in strips[y]) for y in (top, top + 1)]


async def render_at(make_app, width: int, monkeypatch, version: str | None = None) -> tuple[list[str], bool]:
    monkeypatch.setattr(footer_telemetry.Sampler, "sample", lambda self:
                        footer_telemetry.Reading(cpu=25, ram=50))
    if version is not None:
        monkeypatch.setattr(widgets.ContextFooter, "VERSION_TEXT", f"v{version}")
    a = configure(make_app())
    async with a.run_test(size=(width, ROWS)) as pilot:
        a.ctx_max = 200_000
        a.ctx_used = 54_321
        await pilot.pause(1.5)
        shown = a.query_one(".footer-version").display
        return footer_rows(a), shown


def test_footer_text_is_the_single_version_source():
    assert widgets.ContextFooter.VERSION_TEXT == f"v{__version__}"


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [160, 100])
async def test_version_sits_at_the_right_edge_of_the_lower_row(make_app, width, monkeypatch):
    rows, shown = await render_at(make_app, width, monkeypatch)
    status, lower = rows
    print(f"\n[{width}]\n|{status}|\n|{lower}|")
    assert shown
    assert lower.rstrip().endswith(f"v{__version__}"), lower
    assert "ctx" in status or "%" in status, "status line must keep its context readout"


@pytest.mark.asyncio
async def test_version_follows_the_source_with_no_second_literal(make_app, monkeypatch):
    rows, _ = await render_at(make_app, 120, monkeypatch, version="9.9.9")
    assert rows[1].rstrip().endswith("v9.9.9"), rows[1]


@pytest.mark.asyncio
async def test_version_drops_first_at_narrow_widths_without_clipping(make_app, monkeypatch):
    version = f"v{__version__}"
    rows, shown = await render_at(make_app, 60, monkeypatch)
    assert shown and rows[1].rstrip().endswith(version), rows[1]
    # At 40 the permission label and the toggle already fill the lower row.
    # The version yields: absent whole, no stub of it, status line untouched.
    rows, shown = await render_at(make_app, 40, monkeypatch)
    assert not shown
    assert "v0" not in rows[1] and version not in rows[1], rows[1]
    assert "27%" in rows[0] and version not in rows[0], rows[0]
