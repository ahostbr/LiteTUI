"""Footer layout preferences share one normalized contract across settings and UI."""

from __future__ import annotations

import json

import pytest
from textual.app import App
from textual.widgets import Input

from litetui import settings as settings_mod
from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen


def test_footer_order_defaults_and_repairs_partial_values():
    assert Settings().footer_order == list(settings_mod.FOOTER_ORDER_DEFAULT)
    assert settings_mod.normalize_footer_order(
        ["tps", "think", "think", "not-a-field"]
    ) == [
        "tps", "think", "authority", "plan", "seat", "bg", "agents",
        "convo", "ctx", "pct",
    ]


def test_settings_load_repairs_an_old_or_hand_edited_footer_order(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"footer_order": ["ctx", "ctx", "unknown", "seat"]}),
        encoding="utf-8",
    )
    loaded = settings_mod.load(tmp_path)
    assert loaded.footer_order == [
        "ctx", "seat", "authority", "plan", "think", "bg", "agents",
        "convo", "pct", "tps",
    ]


@pytest.mark.asyncio
async def test_interface_exposes_and_collects_footer_order():
    class Host(App):
        def on_mount(self):
            self.push_screen(SettingsScreen(Settings()))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        control = body.query_one("#f-footer_order", Input)
        control.value = "tps, pct, ctx, convo, think, seat, plan, authority"
        saved = body._collect()
        assert saved.footer_order == [
            "tps", "pct", "ctx", "convo", "think", "seat", "plan",
            "authority", "bg", "agents",
        ]
