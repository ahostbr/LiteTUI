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


def test_store_provenance_replays_with_the_prompt_record_only(tmp_path):
    path = tmp_path / "convo.jsonl"
    prompt = {"role": "system", "content": "original"}
    fact = {"version": 1, "mode": "home", "folder": str(tmp_path)}
    snapshot = {"type": "snapshot", "messages": [prompt], "store_identity": fact}

    def replay(*following):
        records = [{"type": "meta", "model": "fixture"}, snapshot, *following]
        path.write_bytes(("\n".join(json.dumps(r) for r in records) + "\n").encode())
        return ConversationRepository.read(path)

    meta, messages = replay()
    assert meta["store_identity"] == fact and messages == [prompt]
    # A torn replacement cannot publish either half of the new pair.
    with path.open("ab") as handle:
        handle.write(b'{"type":"snapshot","store_identity":{"version":2},"messages":')
    assert ConversationRepository.read(path) == (meta, messages)
    changed = {"role": "system", "content": "replacement"}
    meta, messages = replay({"type": "edit", "index": 0, "message": changed,
                             "store_identity": fact})
    assert meta["store_identity"] == fact and messages == [changed]
    for replacement in (
        {"type": "snapshot", "messages": [changed]},
        {"type": "edit", "index": 0, "message": changed},
        {"type": "truncate", "keep_from": 1, "keep_system": False, "prepend": [changed]},
    ):
        meta, messages = replay(replacement)
        assert "store_identity" not in meta and messages == [changed]
    meta, messages = replay({"type": "truncate", "keep_from": 1,
                             "keep_system": True, "prepend": []})
    assert meta["store_identity"] == fact and messages == [prompt]


def test_prompt_writers_include_provenance_in_same_record(tmp_path):
    store = ConversationRepository()
    records = []
    store.write_record = records.append
    fact = {"version": 1, "mode": "owner", "folder": str(tmp_path)}
    prompt = {"role": "system", "content": "composed"}
    store.record_snapshot([prompt], store_identity=fact)
    store.record_edit(0, prompt, store_identity=fact)
    assert len(records) == 2
    assert records[0]["messages"] == [prompt] and records[0]["store_identity"] == fact
    assert records[1]["message"] == prompt and records[1]["store_identity"] == fact
