from pathlib import Path
import pytest
from litetui.agent_launcher import LaunchBlocked


def test_storage_requires_actual_materialized_transcript(tmp_path):
    from litetui.agent_storage import conversation_evidence
    with pytest.raises(LaunchBlocked, match='transcript'):
        conversation_evidence(tmp_path, 'actual-conversation')


def test_storage_resolves_existing_conversation_without_copying(tmp_path):
    from litetui.agent_storage import conversation_evidence
    directory = tmp_path / '.convos' / 'actual-conversation'
    directory.mkdir(parents=True)
    transcript = directory / 'convo.jsonl'
    transcript.write_text('{"role":"user","content":"hello"}\n', encoding='utf-8')
    settings = directory / 'settings.json'
    settings.write_text('{}', encoding='utf-8')
    assert conversation_evidence(tmp_path, 'actual-conversation') == {
        'conversation_dir': str(directory.resolve()),
        'transcript': str(transcript.resolve()),
        'settings': str(settings.resolve()),
    }


@pytest.mark.parametrize('identity', ['../escape', '', None])
def test_storage_rejects_untrusted_identity(tmp_path, identity):
    from litetui.agent_storage import conversation_evidence
    with pytest.raises((ValueError, LaunchBlocked)):
        conversation_evidence(tmp_path, identity)


def test_storage_requires_existing_settings(tmp_path):
    from litetui.agent_storage import conversation_evidence
    directory = tmp_path / '.convos' / 'child'
    directory.mkdir(parents=True)
    (directory / 'convo.jsonl').write_text('{}\n', encoding='utf-8')
    with pytest.raises(LaunchBlocked, match='settings'):
        conversation_evidence(tmp_path, 'child')
