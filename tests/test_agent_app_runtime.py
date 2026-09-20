from types import SimpleNamespace
import pytest
from litetui.agent_launcher import validate_request, LaunchBlocked
from litetui.agent_registry import AgentRegistry
from litetui.agent_inbox import AgentInbox
from litetui.agent_receipts import ParentReceipts
from litetui.agent_app_runtime import run_for_app


@pytest.mark.asyncio
async def test_actual_runtime_routes_to_launch_chat_after_user_switch(tmp_path):
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    storage = tmp_path / '.convos' / 'child-convo'
    storage.mkdir(parents=True)
    (storage / 'convo.jsonl').write_text('{}\n')
    (storage / 'settings.json').write_text('{}')
    starts = []
    app = SimpleNamespace(convo_id='original', store=SimpleNamespace(
        convo_id='original', owned=True, pending=False, loading=False),
        _start_child_delivery=lambda **kw: starts.append(kw))
    class Process:
        conversation_id = 'child-convo'
        async def start_python(self, **kw):
            assert registry.parent_conversation('parent', 'child') == 'original'
        async def rpc_handshake(self, spec, **kw):
            return {'conversation_id': 'child-convo', 'pid': 123, 'process_created': 'stamp'}
        async def send_prompt(self, text): app.convo_id = 'switched'
        async def collect_turn(self, **kw): return {'status': 'completed', 'summary': 'done'}
        async def close(self): return True
    spec = validate_request({'prompt': 'task', 'backend': 'codex', 'model': 'model',
                             'workspace': str(tmp_path)}, parent_profile='autonomous', depth=0)
    ident = await run_for_app(app, spec, Process(), registry=registry, inbox=inbox,
        receipts=receipts, parent='parent', child_id='child', workspace=tmp_path,
        data_root=tmp_path, branch=None, evidence=[], supported_levels=[])
    assert len(starts) == 1
    assert not inbox.pending('parent')
    assert receipts.pending_for_conversation('parent', 'original')[0]['completion_id'] == ident
    assert receipts.pending_for_conversation('parent', 'switched') == []
    assert not registry.active('parent')


@pytest.mark.asyncio
async def test_unowned_parent_refused_before_install_or_spawn():
    app = SimpleNamespace(convo_id='original', store=SimpleNamespace(
        convo_id='original', owned=False, pending=False, loading=False))
    with pytest.raises(LaunchBlocked, match='own'):
        await run_for_app(app, None, None, registry=None, inbox=None, receipts=None,
            parent='parent', child_id='child', workspace=None, data_root=None,
            branch=None, evidence=[], supported_levels=[])


@pytest.mark.asyncio
async def test_textual_parent_timer_applies_real_durable_outcome(tmp_path):
    from textual.app import App
    from litetui.app import LiteTUI
    from litetui.conversation import ConversationRepository
    from litetui.agent_parent_delivery import receipt_message
    store = ConversationRepository()
    store.convo_id = 'parent-convo'
    store.convo_dir = tmp_path / '.convos' / 'parent-convo'
    store.convo_dir.mkdir(parents=True)
    store.convo_path = store.convo_dir / 'convo.jsonl'
    store.convo_path.write_text('{"type":"meta","id":"parent-convo"}\n')
    store.acquire()
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    registry.claim('parent', 'child', limit=1, parent_conversation='parent-convo')
    registry.bind('parent', 'child', conversation_id='child-convo', pid=123, created='stamp')
    outcome = {'child_id': 'child', 'conversation_id': 'child-convo', 'status': 'completed',
               'summary': 'result', 'cleanup': {'state': 'confirmed'}, 'evidence': []}
    ident = inbox.persist('parent', outcome)
    class Host(App):
        _start_child_delivery = LiteTUI._start_child_delivery
        _apply_child_receipts = LiteTUI._apply_child_receipts
        def _chat_running(self): return self.busy
        def _system(self, text): self.notices.append(text)
    app = Host()
    app.store, app.convo_id, app.conversation = store, store.convo_id, []
    app.busy, app.notices = True, []
    try:
        async with app.run_test() as pilot:
            app._start_child_delivery(parent='parent', receipts=receipts, inbox=inbox, registry=registry)
            await pilot.pause(1.15)
            assert not inbox.pending('parent')
            assert receipts.pending('parent')
            assert not app.conversation
            app.busy = False
            await pilot.pause(1.05)
            assert not receipts.pending('parent')
            assert app.conversation == [receipt_message({'completion_id': ident, 'result': outcome})]
            assert store.read(store.convo_path)[1] == app.conversation
            assert len(app.notices) == 1
    finally:
        store.release()