import json
from litetui.conversation import ConversationRepository


def test_nonobject_records_and_invalid_snapshot_do_not_destroy_history(tmp_path):
    path = tmp_path / 'convo.jsonl'
    valid = {'role': 'user', 'content': 'keep me'}
    records = [{'type': 'snapshot', 'messages': [valid]}, [], None, 42,
               {'type': 'snapshot', 'messages': 'not messages'}]
    path.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
    meta, messages = ConversationRepository.read(path)
    assert messages == [valid]


def test_card_summary_reader_ignores_nonobject_records(tmp_path):
    path = tmp_path / 'convo.jsonl'
    path.write_text('["card_summary"]\n{"type":"card_summary","key":"k","summary":"keep"}\n', encoding='utf-8')
    assert ConversationRepository.card_summaries(path) == {'k': 'keep'}
