"""Footer layout preferences share one normalized contract across settings and UI."""

from __future__ import annotations

import json

import pytest
from textual.app import App
from textual.widgets import Input

from litetui import app as app_mod
from litetui import settings as settings_mod
from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen


def test_footer_order_defaults_and_repairs_partial_values():
    assert Settings().footer_order == list(settings_mod.FOOTER_ORDER_DEFAULT)
    assert settings_mod.normalize_footer_order(
        ["tps", "think", "think", "not-a-field"]
    ) == [
        "tps", "think", "pct", "ctx", "seat", "convo", "model",
        "cache", "bg", "agents", "authority", "plan",
    ]


def test_settings_load_repairs_an_old_or_hand_edited_footer_order(tmp_path):
    (tmp_path / "settings.json").write_text(
        json.dumps({"footer_order": ["ctx", "ctx", "unknown", "seat"]}),
        encoding="utf-8",
    )
    loaded = settings_mod.load(tmp_path)
    assert loaded.footer_order == [
        "ctx", "seat", "pct", "convo", "model", "think", "cache",
        "tps", "bg", "agents", "authority", "plan",
    ]


@pytest.mark.parametrize(
    "saved_order",
    [
        ["authority", "plan", "seat", "think", "bg", "agents",
         "convo", "ctx", "pct", "tps"],
        ["authority", "plan", "seat", "think", "bg", "agents",
         "convo", "ctx", "pct", "cache", "tps"],
        ["authority", "plan", "seat", "think", "bg", "agents",
         "convo", "ctx", "pct", "tps", "cache"],  # exact shared-file list
        ["authority", "plan", "seat", "think", "bg", "agents",
         "convo", "ctx", "pct", "tps", "cache", "model"],
        ["authority", "plan", "seat", "think", "bg", "agents",
         "convo", "ctx", "pct", "cache", "tps", "model"],
    ],
)
@pytest.mark.asyncio
async def test_historical_default_loads_and_renders_context_first(tmp_path, saved_order):
    (tmp_path / "settings.json").write_text(
        json.dumps({"footer_order": saved_order}), encoding="utf-8"
    )
    loaded = settings_mod.load(tmp_path)
    assert loaded.footer_order == list(settings_mod.FOOTER_ORDER_DEFAULT)
    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app._apply_context_length = lambda: None
    app.settings = loaded
    app.ctx_used, app.ctx_max = 23_133, 120_064
    async with app.run_test(size=(200, 24)) as pilot:
        await pilot.pause()
        footer = app.ctx_label_text.plain
        assert footer.index("19%") < footer.index("ctx ") < footer.index("unregistered"), footer


def test_custom_complete_footer_order_is_preserved_on_load(tmp_path):
    custom = ["seat", "ctx", "pct", "think", "convo", "tps", "cache",
              "bg", "agents", "authority", "plan", "model"]
    (tmp_path / "settings.json").write_text(
        json.dumps({"footer_order": custom}), encoding="utf-8"
    )
    assert settings_mod.load(tmp_path).footer_order == custom


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
            "authority", "model", "cache", "bg", "agents",
        ]
