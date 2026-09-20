from litetui.agent_receipts import ParentReceipts


def ready(path):
    receipts = ParentReceipts(path)
    receipts.accept_for_conversation('parent', 'chat', {'completion_id': 'completion', 'result': {
        'child_id': 'child', 'conversation_id': 'child-chat', 'status': 'completed',
        'summary': 'done', 'cleanup': {}, 'evidence': []}})
    return receipts


def test_wake_requires_applied_history_and_matching_conversation(tmp_path):
    r = ready(tmp_path / 'receipts.sqlite')
    assert r.claim_wake('parent', 'chat') == []
    r.mark_applied('parent', 'completion')
    assert r.claim_wake('other', 'chat') == []
    assert r.claim_wake('parent', 'other') == []
    assert r.claim_wake('parent', 'chat') == ['completion']
    assert r.claim_wake('parent', 'chat') == []


def test_interrupted_wake_is_visible_not_automatically_reexecuted(tmp_path):
    path = tmp_path / 'receipts.sqlite'
    r = ready(path)
    r.mark_applied('parent', 'completion')
    assert r.claim_wake('parent', 'chat') == ['completion']
    reopened = ParentReceipts(path)
    assert reopened.claim_wake('parent', 'chat') == []
    assert reopened.uncertain_wakes('parent', 'chat') == ['completion']
    assert reopened.finish_wake('parent', 'chat', ['completion'])
    assert reopened.uncertain_wakes('parent', 'chat') == []


def test_concurrent_claims_only_one_consumer(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'receipts.sqlite'
    r = ready(path)
    r.mark_applied('parent', 'completion')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: ParentReceipts(path).claim_wake('parent', 'chat'), range(4)))
    assert sum(len(result) for result in results) == 1
