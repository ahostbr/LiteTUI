"""Settings that configure the engine LiteTUI launches belong to the machine, not to one conversation.

Ryan 2026-10-24: context set to 100K in Settings for Strata, engine reloaded, LiteTUI reloaded, still 150K.
The value was saved into the conversation that was open; a relaunch starts a new conversation, born from the
global defaults, so the engine launched at the model's own 150000 every time.
"""
import uuid

import pytest

from litetui import settings, strata_engine
from litetui.agent_launch_context import ordinary
from litetui.settings_scope import SETTING_SPECS, SettingScope
from litetui.settings_service import SettingChange, SettingsService

ENGINE_LAUNCH = ("ninfer_executable ninfer_artifact ninfer_max_context ninfer_max_concurrency ninfer_kv_dtype "
                 "ninfer_kv_capacity ninfer_host_kv_mib strata_root strata_config strata_max_context "
                 "xolotl_root xolotl_config xolotl_max_context").split()


@pytest.mark.parametrize("key", ENGINE_LAUNCH)
def test_engine_launch_settings_are_device_scoped_and_apply_on_restart(key):
    spec = SETTING_SPECS[key]
    assert spec.scope == SettingScope.DEVICE and spec.apply_timing == "restart"


def test_the_connection_endpoints_stay_with_the_conversation():
    for key in ("ninfer_host", "strata_host", "xolotl_host"):
        assert SETTING_SPECS[key].scope == SettingScope.CONVERSATION


def test_a_strata_context_saved_in_one_conversation_reaches_the_next_one_and_the_engine_command(tmp_path):
    cfg = settings.Settings(default_model="fixture-model")
    with ordinary(tmp_path, cfg) as session:
        service = SettingsService(tmp_path, agent_session=session)
        first, second = str(uuid.uuid4()), str(uuid.uuid4())
        snapshot = service.create_conversation(first)
        service.save_patch(first, [SettingChange("strata_max_context", 100000, "device")], snapshot.revisions)

        later = service.create_conversation(second).effective          # a relaunch / new conversation
        assert later.strata_max_context == 100000

    # ... and the launcher turns exactly that value into the engine's flag.
    base = {"args": ["--pack", "p", "--max-context", "150000", "--kv", "int8"], "cwd": ".", "exe": "e"}
    run = strata_engine.with_context(base, later.strata_max_context)
    assert run["args"][run["args"].index("--max-context") + 1] == "100000"
