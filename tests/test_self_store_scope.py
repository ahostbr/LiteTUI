from litetui import tool_policy as policy, paths


def test_unknown_conversation_does_not_grant_all_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path / '.convos')
    caps = set(policy.classify_write({'path': str(tmp_path / '.convos' / 'other' / 'memory.md')}, tmp_path))
    assert policy.SELF_STORE not in caps


def test_only_active_owned_store_files_get_self_store(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path / '.convos')
    own = tmp_path / '.convos' / 'a'
    for relative, expected in [('memory.md', True), ('memories/topic.md', True),
                               ('settings.json', False), ('convo.jsonl', False),
                               ('../b/memory.md', False)]:
        decision = policy.evaluate(policy.STRICT, policy.WRITE_POLICY,
              {'path': str(own / relative)}, tmp_path, active_conversation=own)
        assert (policy.SELF_STORE in decision.capabilities) == expected
