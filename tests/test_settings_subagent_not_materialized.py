"""A conversation save never materializes subagent_model into the conversation.

convo_settings.from_global leaves the subagent route out of a conversation's execution on
purpose (af595454: "Legacy model-only defaults must stay inherited, not become sticky
overrides"). settings_service's legacy materialization used to fill every missing
conversation-scope key from the inherited settings, subagent_model included, so ANY save into
a conversation wrote "subagent_model": null back into it.
"""
import json

from litetui import settings
from litetui.agent_launch_context import ordinary
from litetui.settings_service import SettingChange, SettingsService


def test_a_conversation_save_leaves_the_subagent_route_inherited(tmp_path):
    cfg = settings.Settings()
    cfg.default_model = 'old-fixture'
    with ordinary(tmp_path, cfg) as session:
        service = SettingsService(tmp_path, agent_session=session)
        cid = session.initial_conversation_id
        for model in ('new-fixture', 'newer-fixture'):
            snap = service.snapshot(cid)
            result = service.save_patch(
                cid, [SettingChange('default_model', model, 'conversation')], snap.revisions)
            assert all(row.saved for row in result.persistence), result
            path = service._paths(cid)['conversation']
            execution = json.loads(path.read_text(encoding='utf-8')).get('execution', {})
            assert execution.get('default_model') == model
            assert 'subagent_model' not in execution, execution
