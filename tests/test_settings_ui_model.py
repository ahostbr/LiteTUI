from litetui.settings_ui_model import (
    SETTINGS_SECTIONS,
    SETTINGS_TABS,
    search_settings,
    settings_sections_for_tab,
)


def test_every_proposed_tab_has_named_sections():
    expected_tabs = {
        "model",
        "ninfer",
        "voice",
        "generation",
        "agent",
        "compaction",
        "capabilities",
        "hooks",
        "themes",
        "interface",
    }
    assert {section.tab_id for section in SETTINGS_SECTIONS} == expected_tabs
    assert {tab.tab_id for tab in SETTINGS_TABS} == expected_tabs
    assert all(tab.section_ids for tab in SETTINGS_TABS)
    assert all(settings_sections_for_tab(tab) for tab in expected_tabs)
    assert len({section.section_id for section in SETTINGS_SECTIONS}) == len(SETTINGS_SECTIONS)


def test_search_matches_help_and_returns_scope_metadata():
    hits = search_settings("footer order left to right")
    assert [hit.section_id for hit in hits] == ["interface-footer"]
    assert "footer_order" in hits[0].field_names
    assert hits[0].scopes == ("app",)

    hits = search_settings("sidebar")
    assert [hit.section_id for hit in hits] == ["interface-dialogs"]
    assert hits[0].field_names == ("dialog_style", "dialog_side")


def test_search_can_be_limited_to_active_tab():
    assert search_settings("router", active_tab="interface") == ()
    assert search_settings("model", active_tab="model")
