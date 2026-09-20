from dataclasses import replace

from litetui.settings import Settings
from litetui.settings_draft import SettingsDraft


def test_draft_deep_copies_nested_settings():
    source = Settings(
        llama_models_dirs=["C:/models"],
        mcp_disabled_servers=["calendar"],
        custom_themes={"solar": {"accent": "gold"}},
    )
    draft = SettingsDraft(source)

    draft.working.llama_models_dirs.append("D:/models")
    draft.working.mcp_disabled_servers.clear()
    draft.working.custom_themes["solar"]["accent"] = "blue"

    assert source.llama_models_dirs == ["C:/models"]
    assert source.mcp_disabled_servers == ["calendar"]
    assert source.custom_themes["solar"]["accent"] == "gold"
    assert {"llama_models_dirs", "mcp_disabled_servers", "custom_themes"} <= set(draft.dirty_fields())


def test_draft_updates_and_replaces_working_copy():
    source = Settings(temperature=0.4)
    draft = SettingsDraft(source)
    draft.update("temperature", 0.9)
    assert draft.working.temperature == 0.9
    assert draft.dirty_fields() == ("temperature",)

    replacement = replace(source, temperature=0.2)
    draft.replace_working(replacement)
    assert draft.working.temperature == 0.2
    assert draft.dirty_fields() == ("temperature",)
