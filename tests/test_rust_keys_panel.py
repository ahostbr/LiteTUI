"""/keys for the Rust frontend: native_keys.rows is Textual's own key panel table."""
from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich import box
from rich.console import Console
from rich.table import Table
from textual.app import App
from textual.widgets import Input, TextArea
from textual.widgets._key_panel import BindingsTable

from litetui import native_keys


class Fixture(App):
    def compose(self):
        yield Input()
        yield TextArea()


def plain(table, width):
    out = StringIO()
    Console(file=out, width=width, color_system=None, legacy_windows=False).print(table)
    return out.getvalue()


def table_from(rows):
    # The Rust panel's model of the table: Rich SIMPLE, no header, padding 0, key right.
    table = Table(padding=(0, 0), show_header=False, box=box.SIMPLE)
    table.add_column("", justify="right")
    for row in rows:
        text = " ".join(part for part in (row["text"], row["tooltip"]) if part)
        table.add_row(row["key"], text, end_section=row["end_section"])
    return table


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", [Input, TextArea])
async def test_rows_draw_the_table_textuals_key_panel_draws(focus):
    app = Fixture()
    async with app.run_test(size=(140, 45)) as pilot:
        app.query_one(focus).focus()
        await pilot.pause()
        rows = native_keys.rows(app)
        app.action_show_help_panel()
        await pilot.pause()
        textual_table = app.screen.query_one(BindingsTable).render_bindings_table()
        assert rows and any(r["end_section"] for r in rows)
        assert any(r["title"] for r in rows) == (focus is Input)  # Input's "Input" group title
        for width in (41, 25, 60):
            assert plain(table_from(rows), width) == plain(textual_table, width)


def test_no_screen_means_no_table():
    assert native_keys.rows(SimpleNamespace(_screen_stack=[])) is None
