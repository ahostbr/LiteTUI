from types import SimpleNamespace
import pytest
from litetui.conversation import ConversationRepository
from litetui.agent_receipts import ParentReceipts
from litetui.agent_parent_delivery import apply_receipts, receipt_message


@pytest.fixture
def state(tmp_path):
    store = ConversationRepository()
    store.convo_id = 'original'
    store.convo_dir = tmp_path / '.convos' / 'original'
    store.convo_dir.mkdir(parents=True)
    store.convo_path = store.convo_dir / 'convo.jsonl'
    store.convo_path.write_text('{"type":"meta","id":"original"}\n')
    store.acquire()
    app = SimpleNamespace(store=store, convo_id='original', conversation=[], _chat_running=lambda: False)
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    event = {'completion_id': 'completion', 'result': {
        'child_id': 'child', 'conversation_id': 'child-convo', 'status': 'failed',
        'summary': 'tool failed', 'cleanup': {'state': 'confirmed'}, 'evidence': []}}
    receipts.accept_for_conversation('parent', 'original', event)
    yield app, receipts, event
    store.release()


def test_commits_disk_and_live_history_without_duplicate_append(state):
    app, receipts, event = state
    assert apply_receipts(app, parent='parent', receipts=receipts) == ['completion']
    assert app.conversation == [receipt_message(event)]
    assert app.store.read(app.store.convo_path)[1] == app.conversation
    assert apply_receipts(app, parent='parent', receipts=receipts) == []


@pytest.mark.parametrize('blocked', ['busy', 'switched', 'unsaved', 'quit'])
def test_unsafe_boundary_leaves_receipt_pending(state, blocked):
    app, receipts, event = state
    if blocked == 'busy': app._chat_running = lambda: True
    if blocked == 'switched': app.convo_id = 'other'
    if blocked == 'unsaved': app.conversation.append({'role': 'user', 'content': 'unsaved'})
    if blocked == 'quit': app._gui_quitting = True
    assert apply_receipts(app, parent='parent', receipts=receipts) == []
    assert receipts.pending('parent') == [event]
    assert app.store.read(app.store.convo_path)[1] == []


def test_failed_marker_retry_does_not_duplicate_live_or_resumed_history(state, monkeypatch):
    app, receipts, event = state
    original = receipts.mark_applied
    def fail(*args): raise OSError('marker failed')
    monkeypatch.setattr(receipts, 'mark_applied', fail)
    with pytest.raises(OSError): apply_receipts(app, parent='parent', receipts=receipts)
    assert app.conversation == [receipt_message(event)]
    monkeypatch.setattr(receipts, 'mark_applied', original)
    assert apply_receipts(app, parent='parent', receipts=receipts) == ['completion']
    assert app.conversation == [receipt_message(event)]


def test_history_write_failure_does_not_change_live_context(state, monkeypatch):
    app, receipts, event = state
    def fail(*args): raise OSError('disk full')
    monkeypatch.setattr(app.store, 'commit_child_message', fail)
    with pytest.raises(OSError): apply_receipts(app, parent='parent', receipts=receipts)
    assert app.conversation == []
    assert receipts.pending('parent') == [event]
