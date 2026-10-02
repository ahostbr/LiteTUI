"""Owned picker/new-process boundary and task write confinement; fixture-only."""
from types import SimpleNamespace
import json

import pytest

from litetui.agent_launch_context import create
from litetui.agent_store import AgentStore
from litetui.plugins import convo
from litetui import tasks, agent_seat_launch, paths

AID = '11111111-1111-4111-8111-111111111111'
BID = '22222222-2222-4222-8222-222222222222'
CID = '33333333-3333-4333-8333-333333333333'


def home(root, name, identity):
    return create(root, name, agent_id=identity, backend='codex', model='fixture', thinking_level='high')


def transcript(session, content):
    target = session.conversation_directory(CID) / 'convo.jsonl'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({'type': 'msg', 'message': {'role': 'user', 'content': content}}) + '\n')
    return target


def test_picker_is_agent_first_and_same_agent_resume_stays_owned(tmp_path, monkeypatch):
    with home(tmp_path, 'QuietHelm', AID) as session:
        target = transcript(session, 'own conversation')
        app = SimpleNamespace(_agent_session=session, store=SimpleNamespace(persist_error=None),
                              convo_path=target, system_message=lambda _: None)
        resumed, picks = [], []
        app._on_convo_picked = resumed.append
        monkeypatch.setattr(convo, 'pick', lambda app, title, items, callback, **kw:
                            picks.append((title, items, callback)))
        convo._open_convos_picker(app)
        assert picks[0][0] == 'Choose an agent'
        assert picks[0][1] == [(AID, 'QuietHelm')]
        picks[0][2](AID)
        assert picks[1][1][0][0] == str(target)
        picks[1][2](str(target))
        assert resumed == [str(target)]


@pytest.mark.asyncio
async def test_other_agent_picker_explicit_action_launches_not_hot_switch(tmp_path, monkeypatch):
    with home(tmp_path, 'OtherAgent', BID) as other:
        transcript(other, 'other conversation')
    with home(tmp_path, 'QuietHelm', AID) as session:
        original = session.authority
        messages, picks, coroutines, opened = [], [], [], []
        app = SimpleNamespace(_agent_session=session, store=SimpleNamespace(persist_error=None),
             system_message=messages.append, run_worker=lambda coro, **kw: coroutines.append(coro))
        monkeypatch.setattr(convo, 'pick', lambda app, title, items, callback, **kw:
                            picks.append((title, items, callback)))
        async def launch(app, identity, conversation):
            opened.append((identity, conversation))
            return 'fixture own-seat launch accepted'
        monkeypatch.setattr(agent_seat_launch, 'open_seat', launch)
        convo._open_convos_picker(app)
        picks[0][2](BID)
        picks[1][2](CID)
        assert picks[2][1] == [('open', 'Open OtherAgent in its own seat')]
        assert not opened and not coroutines
        picks[2][2]('open')
        await coroutines[0]
        assert opened == [(BID, CID)]
        assert session.authority == original


@pytest.mark.asyncio
async def test_open_seat_actual_subprocess_seam_argv_root_and_refusal(tmp_path, monkeypatch):
    with home(tmp_path, 'OtherAgent', BID) as other:
        transcript(other, 'separate')
    with home(tmp_path, 'QuietHelm', AID) as session:
        app = SimpleNamespace(_agent_session=session,
             seat=SimpleNamespace(registered=True, agent_id=AID, tier='worker'))
        calls = []
        class Process:
            returncode = 0
            async def communicate(self):
                return b'fixture launched', b''
        async def spawn(*args, **kw):
            calls.append((args, kw))
            return Process()
        monkeypatch.setattr(agent_seat_launch.asyncio, 'create_subprocess_exec', spawn)
        assert await agent_seat_launch.open_seat(app, BID, CID) == 'fixture launched'
        args, kw = calls[0]
        assert args[1:4] == ('-m', 'liteharness.cli', 'spawn')
        assert args[args.index('--resume') + 1] == 'OtherAgent'
        assert args[args.index('--convo') + 1] == CID
        assert kw['env']['LITETUI_DATA_ROOT'] == str(tmp_path)
        assert session.authority.agent_id == AID


def test_owned_task_save_never_uses_archive_twin_or_legacy_topup(tmp_path, monkeypatch):
    with home(tmp_path, 'QuietHelm', AID) as session:
        directory = session.conversation_directory(CID)
        directory.mkdir()
        archive = tmp_path / '.convos' / CID
        archive.mkdir(parents=True)
        (archive / tasks.STORE).write_bytes(b'foreign archive evidence')
        monkeypatch.setattr(paths, 'CONVO_DIR', tmp_path / '.convos')
        monkeypatch.setattr(tasks, 'topup', lambda *_: pytest.fail('implicit copy on owned bind'))
        task = tasks.new_task('fixture', {}, CID)
        tasks.save_by_convo([task], agent_session=session)
        assert (directory / tasks.STORE).is_file()
        assert (archive / tasks.STORE).read_bytes() == b'foreign archive evidence'
        held = {}
        tasks.bind(held, directory, tmp_path, agent_session=session)
        assert task.id in held
        with pytest.raises(ValueError, match='outside'):
            tasks.bind(held, archive, tmp_path, agent_session=session)
