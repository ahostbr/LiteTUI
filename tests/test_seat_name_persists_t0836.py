"""T0836 -- Ryan's seat name came back as "DELETE" after he saved a new one.

Ryan, 2026-11-04: "its not saving the agent id when i change it .... keeps
loading to DELETE even though i saved and changed it".

Two defects, one symptom. Every arm uses a temp root: the live settings.json at
the repo root belongs to a running seat.

1. SAVE NEVER REACHED THE FILE THE NEXT LAUNCH READS. `seat_name` was filed as a
   CONVERSATION setting, so the dialog wrote it into that one conversation's
   snapshot, while `agent_launch_context.ordinary` names the next seat from the
   shared settings.json. The settings UI model already filed it under "defaults";
   the scope table disagreed with it (two tables that must agree).
2. AN AGENT SEAT WROTE ITS OWN NAME INTO THE SHARED FILE. `apply_settings` copies
   the agent's name/backend/model/thinking onto the Settings AFTER `load()` took
   the baseline `save()` diffs against, so the first unrelated save from any
   agent seat looked like a change to those keys and wrote them.
"""
import json

import pytest

from litetui import agent_launch_context, agent_store
from litetui import settings as st
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingChange, SettingsService
from litetui.settings_ui_model import SETTINGS_SECTIONS

AID = '11111111-1111-4111-8111-111111111111'


def _read(root):
    return json.loads(st.settings_path(root).read_text(encoding='utf-8'))


@pytest.fixture
def agent_root(tmp_path):
    directory = tmp_path / '.agents' / 'Worker'
    directory.mkdir(parents=True)
    (directory / 'settings.json').write_text(json.dumps({
        'schema_version': 1, 'name': 'Worker', 'agent_id': AID,
        'execution': {'backend': 'ninfer', 'model': 'agent-model', 'thinking_level': 'xhigh'},
    }))
    st.save(st.Settings(seat_name='Ryan', backend='codex', default_model='ryan-model',
                        thinking_level='medium'), tmp_path)
    return tmp_path


def test_an_agent_seat_does_not_write_its_identity_into_the_shared_settings(agent_root):
    shared_before = _read(agent_root)
    settings = st.load(agent_root)  # what an agent seat's App does first
    with agent_launch_context.acquire(agent_root, 'Worker') as session:
        agent_launch_context.apply_settings(session, settings)
        assert settings.default_model == 'agent-model', 'CONTROL: the agent seat runs on its own authority'
        settings.show_thinking = not settings.show_thinking  # any unrelated save
        st.save(settings, agent_root)
    shared = _read(agent_root)
    for key in ('seat_name', 'backend', 'default_model', 'thinking_level'):
        assert shared[key] == shared_before[key], f'an agent seat wrote its {key} into Ryan\'s settings'
    assert shared['show_thinking'] == settings.show_thinking, 'CONTROL: the real change was saved'


def test_a_saved_seat_name_reaches_the_file_the_next_launch_reads(tmp_path):
    cfg = st.Settings(default_model='fixture-model')
    with agent_launch_context.ordinary(tmp_path, cfg) as session:
        service = SettingsService(tmp_path, agent_session=session)
        cid = session.initial_conversation_id
        service.create_conversation(cid)
        scope = SETTING_SPECS['seat_name'].scope.value
        result = service.save_patch(cid, [SettingChange('seat_name', 'RyanSeat', scope)],
                                    service.snapshot(cid).revisions)
        assert result.fully_saved, 'CONTROL: the save itself succeeded'
    assert st.load(tmp_path).seat_name == 'RyanSeat'
    with agent_launch_context.ordinary(tmp_path, st.load(tmp_path)) as relaunch:
        assert relaunch.authority.name == 'RyanSeat'


def test_scope_table_and_ui_model_agree_on_seat_name():
    ui_scope = next(f.scope for sec in SETTINGS_SECTIONS for f in sec.fields if f.name == 'seat_name')
    assert SETTING_SPECS['seat_name'].scope.value == ui_scope
