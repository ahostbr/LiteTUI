from types import SimpleNamespace
import subprocess
import pytest
from litetui.agent_launch_service import launch_for_app
from litetui.agent_registry import AgentRegistry
from litetui.agent_inbox import AgentInbox
from litetui.agent_receipts import ParentReceipts


@pytest.mark.asyncio
async def test_composed_launch_prepares_isolation_and_transfers_outcome(tmp_path):
    from pathlib import Path
    repo = tmp_path / 'parent'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git('init'); git('config', 'user.name', 'fixture'); git('config', 'user.email', 'fixture@example.invalid')
    (repo / 'answer.py').write_text('VALUE = 41\n')
    git('add', '.'); git('commit', '-m', 'baseline')
    baseline = git('rev-parse', 'HEAD')
    (repo / 'answer.py').write_text('VALUE = 99\n')
    registry = AgentRegistry(tmp_path / 'registry.sqlite')
    inbox = AgentInbox(tmp_path / 'inbox.sqlite')
    receipts = ParentReceipts(tmp_path / 'receipts.sqlite')
    app = SimpleNamespace(convo_id='parent-chat', settings=SimpleNamespace(tool_policy_profile='autonomous'),
        store=SimpleNamespace(convo_id='parent-chat', owned=True, pending=False, loading=False),
        _start_child_delivery=lambda **kw: None)
    class Process:
        conversation_id = 'child-chat'
        async def start_python(self, **kw):
            assert len(registry.active('parent')) == 1
            self.workspace = Path(kw['cwd'])
            assert (self.workspace / 'answer.py').read_text() == 'VALUE = 41\n'
            root = Path(kw['env']['LITETUI_DATA_ROOT']) / '.convos' / self.conversation_id
            root.mkdir(parents=True)
            (root / 'convo.jsonl').write_text('{}\n')
            (root / 'settings.json').write_text('{}')
        async def rpc_handshake(self, spec, **kw):
            return {'conversation_id': self.conversation_id, 'pid': 123, 'process_created': 'stamp'}
        async def send_prompt(self, prompt):
            (self.workspace / 'answer.py').write_text('VALUE = 42\n')
        async def collect_turn(self, **kw): return {'status': 'completed', 'summary': 'fixture changed'}
        async def close(self): return True
    result = await launch_for_app(app, {'prompt': 'task', 'backend': 'codex', 'model': 'model',
        'workspace': str(repo)}, registry=registry, inbox=inbox, receipts=receipts,
        parent='parent', storage=tmp_path / 'children', baseline=baseline,
        supported_levels=[], process_factory=Process)
    assert result['result']['status'] == 'completed'
    assert not registry.active('parent')
    assert not inbox.pending('parent')
    assert receipts.pending_for_conversation('parent', 'parent-chat')[0]['completion_id'] == result['completion_id']
    assert (repo / 'answer.py').read_text() == 'VALUE = 99\n'
    assert (tmp_path / 'children' / result['child_id'] / 'workspace' / 'answer.py').read_text() == 'VALUE = 42\n'
