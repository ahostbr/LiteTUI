from types import SimpleNamespace as NS

import pytest
from textual.app import App

from litetui.settings import Settings
from litetui.settings_screen import (
    SettingsBody,
    SettingsExitConfirm,
    SettingsScreen,
    SettingsSectionHeader,
)
from litetui.settings_ui_model import SETTINGS_SECTIONS


class SettingsHost(App):
    backend = NS(name="lmstudio")
    model_id = "local-model"

    def __init__(self):
        super().__init__()
        self.settings = Settings()

    def on_mount(self):
        self.push_screen(SettingsScreen(self.settings))


@pytest.mark.asyncio
async def test_settings_mounts_one_disclosure_header_per_metadata_section():
    app = SettingsHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        headers = body.query(SettingsSectionHeader)
        mounted_ids = {header.spec.section_id for header in headers}
        declared_ids = {section.section_id for section in SETTINGS_SECTIONS}
        assert mounted_ids <= declared_ids
        assert {"model-connection", "agent-routing", "interface-footer"} <= mounted_ids


@pytest.mark.asyncio
async def test_section_toggle_hides_rows_without_unmounting_controls():
    app = SettingsHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        header = body.query_one("#set-section-model-engine", SettingsSectionHeader)
        assert header._members
        first_member = header._members[0]
        assert first_member.display is False

        await pilot.click("#set-section-model-engine")
        assert first_member.display is True
        assert body.query_one("#f-backend").display is True
        assert body.query_one("#f-backend")


@pytest.mark.asyncio
async def test_search_jumps_to_tab_and_focuses_matching_footer_field():
    app = SettingsHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        search = body.query_one("#set-search")
        search.focus()
        search.value = "footer order"
        await pilot.pause()
        await pilot.pause()

        assert body.query_one("#set-tabs").active == "tab-interface"
        assert "1 section" in str(body.query_one("#set-search-status").render())
        target = body.query_one("#f-footer_order")
        assert target.display is True
        assert target.has_focus


@pytest.mark.asyncio
async def test_dirty_cancel_offers_three_way_choice_and_restore_is_confirmed():
    app = SettingsHost()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        body.query_one("#f-footer_order").value = "seat,think"
        body.action_cancel()
        await pilot.pause()

        assert isinstance(app.screen, SettingsExitConfirm)
        assert app.screen.query_one("#settings-save")
        assert app.screen.query_one("#settings-discard")
        assert app.screen.query_one("#settings-keep")

        await pilot.click("#settings-keep")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)

        body._defaults()
        await pilot.pause()
        assert isinstance(app.screen, SettingsExitConfirm)
        assert app.screen.restore is True


@pytest.mark.asyncio
async def test_every_free_tier_key_row_is_masked_and_under_its_own_header():
    """Ryan 2026-09-24 (liteask a-29b8bd60): "a key field per source in
    /settings". A key on screen is a key in the screenshot, so each is masked."""
    from textual.widgets import Input

    from litetui.sidecar_settings import SECRET_FIELDS

    app = SettingsHost()
    app.settings.groq_api_key = "gsk-shown-masked"
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = app.screen.query_one(SettingsBody)
        header = body.query_one("#set-section-model-free-keys", SettingsSectionHeader)
        for name in SECRET_FIELDS:
            row = body.query_one(f"#f-{name}", Input)
            assert row.password, f"{name} is shown in clear"
            assert any(row in member.query(Input) for member in header._members), f"{name} is not under its header"
        assert body.query_one("#f-groq_api_key", Input).value == "gsk-shown-masked"
