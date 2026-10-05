"""Bounded actual CLI/resume acceptance seams with temp ownership only."""
import json
import sys
from uuid import uuid4

import pytest
from litetui import app as app_module, cli, paths, settings, shared_state, image_viewer
from litetui.agent_launch_context import ordinary, acquire
from litetui.agent_ownership import AgentSession
from litetui.agent_store import AgentStore
from litetui.textfmt import memory_prompt


@pytest.fixture
def cli_world(tmp_path, monkeypatch):
    cfg = settings.Settings(backend='codex', default_model='fixture', thinking_level='high')
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    monkeypatch.setattr(shared_state, 'check_data_version', lambda _: None)
    monkeypatch.setattr(image_viewer, 'init_image_backend', lambda: None)
    monkeypatch.setattr(app_module, 'wants_ansi_fallback', lambda: False)
    seen = []
    class TransportApp:
        def __init__(self, **kwargs):
            session = kwargs['agent_session']
            seen.append((session.authority, session.memory_root, kwargs))
        def run(self):
            pass
    monkeypatch.setattr(app_module, 'LiteTUI', TransportApp)
    return tmp_path, cfg, seen


def invoke(monkeypatch, *args):
    monkeypatch.setattr(sys, 'argv', ['litetui', *args])
    cli.main()


def test_actual_cli_fresh_then_contended_ordinary_owns_exact_homes(cli_world, monkeypatch, capsys):
    root, cfg, seen = cli_world
    invoke(monkeypatch)
    authority, home, _ = seen[-1]
    assert authority.name == 'LiteTUI' and home == root / '.agents' / 'LiteTUI'
    with acquire(root, 'LiteTUI') as holder:
        invoke(monkeypatch)
        second, second_home, _ = seen[-1]
        assert second.name.startswith('LiteTUI-') and second.agent_id != holder.authority.agent_id
        assert second_home == root / '.agents' / second.name
    assert 'Default agent is busy' in capsys.readouterr().err
    assert not (root / '.convos').exists()


@pytest.mark.parametrize('flag,value', [('--backend','claude'),('--model','wrong'),('--thinking-level','low')])
def test_actual_cli_existing_ordinary_divergent_flag_refuses_unchanged(cli_world, monkeypatch, flag, value):
    root, cfg, seen = cli_world
    with ordinary(root, cfg) as session:
        home = session.memory_root
    before = (home / 'settings.json').read_bytes()
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, flag, value)
    assert exc.value.code == 2 and seen == []
    assert (home / 'settings.json').read_bytes() == before
    assert not (root / '.convos').exists()
    with acquire(root, 'LiteTUI'):
        pass


def test_actual_cli_fresh_flags_and_existing_matching_flags(cli_world, monkeypatch):
    root, cfg, seen = cli_world
    flags = ('--backend','claude','--model','sonnet','--thinking-level','low')
    invoke(monkeypatch, *flags)
    authority, home, _ = seen[-1]
    assert (authority.backend, authority.model, authority.thinking_level) == ('claude','sonnet','low')
    before = (home / 'settings.json').read_bytes()
    invoke(monkeypatch, *flags)
    assert seen[-1][0] == authority
    assert (home / 'settings.json').read_bytes() == before
    assert not (root / '.convos').exists()


def test_actual_cli_catalog_read_to_default_reservation_race(cli_world, monkeypatch):
    root, cfg, seen = cli_world
    original = AgentSession.create_fresh.__func__
    winners = []
    def raced(cls, store, **kwargs):
        if kwargs['name'] == 'LiteTUI' and not winners:
            winner = original(cls, store, name='LiteTUI', agent_id=str(uuid4()), backend='codex', model='fixture', thinking_level='high')
            winners.append(winner)
            raise FileExistsError('deterministic competing reservation')
        return original(cls, store, **kwargs)
    monkeypatch.setattr(AgentSession, 'create_fresh', classmethod(raced))
    try:
        invoke(monkeypatch)
        authority, home, _ = seen[-1]
        assert authority.name.startswith('LiteTUI-')
        assert home == root / '.agents' / authority.name
        assert authority.agent_id != winners[0].authority.agent_id
        assert len(AgentStore(root).list_agents()) == 2
        assert not (root / '.convos').exists()
    finally:
        for winner in winners:
            winner.release()


def test_actual_owned_resume_persists_home_address_preserving_snapshot_and_user_prose(tmp_path, monkeypatch):
    cfg = settings.Settings(backend='codex', default_model='fixture', thinking_level='high')
    monkeypatch.setattr(paths, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(settings, 'load', lambda: cfg)
    with ordinary(tmp_path, cfg) as session:
        app = app_module.LiteTUI(agent_session=session)
        app._system = lambda _: None
        app._sync_seat_identity = lambda: None
        app._sync_fleet_identity = lambda: None
        app._refresh_ctx_label = lambda: None
        app._render_resumed = lambda _: None
        cid = str(uuid4())
        old = tmp_path / '.convos' / cid
        old.mkdir(parents=True)
        old_file = old / 'convo.jsonl'
        old_file.write_bytes(b'legacy archive remains unchanged\n')
        snapshot = '\n## Your store, loaded once at the start of this conversation\nSNAPSHOT USER TEXT'
        prompt = 'USER PREAMBLE\n' + memory_prompt(cid, old) + 'UNHEADED USER TAIL' + snapshot
        target = session.conversation_directory(cid) / 'convo.jsonl'
        target.parent.mkdir()
        target.write_text(json.dumps({'type':'meta','id':cid})+'\n'+json.dumps({'type':'msg','message':{'role':'system','content':prompt}})+'\n'+json.dumps({'type':'msg','message':{'role':'user','content':'preserve turn'}})+'\n',encoding='utf-8')
        before_archive = old_file.read_bytes()
        try:
            assert app._resume(target, startup=True)
            current = app.conversation[0]['content']
            assert 'USER PREAMBLE' in current and 'UNHEADED USER TAIL' in current
            assert snapshot in current and str(session.memory_root).replace('\\','/') in current.replace('\\','/')
            assert str(old).replace('\\','/') not in current.replace('\\','/')
            assert app.conversation[1]['content'] == 'preserve turn'
            _, persisted = app_module.ConversationRepository.read(target)
            assert persisted[0]['content'] == current
            assert old_file.read_bytes() == before_archive
            assert list(old.iterdir()) == [old_file]
        finally:
            app.store.release()


def test_actual_legacy_uuid_refusal_has_actionable_readonly_and_operator_receipt_guidance(cli_world, monkeypatch, capsys):
    root, _, seen = cli_world
    cid = str(uuid4())
    legacy = root / '.convos' / cid
    legacy.mkdir(parents=True)
    transcript = legacy / 'convo.jsonl'
    transcript.write_text(json.dumps({'type':'msg','message':{'role':'user','content':'legacy evidence'}})+'\n',encoding='utf-8')
    before = transcript.read_bytes()
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, '--convo', cid)
    assert exc.value.code == 2 and seen == []
    error = capsys.readouterr().err
    diagnostic = error.split('litetui: error:', 1)[1]
    assert 'litetui --export-conversation <archive/convo.jsonl> --export-output <new.md>' in diagnostic
    assert 'operator-approved' in diagnostic and 'hash-pinned manifest/merge receipt' in diagnostic
    assert 'quiet-seat verification' in diagnostic and '--agent does not migrate' in diagnostic
    output = root / 'new.md'
    invoke(monkeypatch, '--export-conversation', str(transcript), '--export-output', str(output))
    assert 'legacy evidence' in output.read_text(encoding='utf-8')
    assert seen == [] and transcript.read_bytes() == before
    assert list(legacy.iterdir()) == [transcript]
    assert not (root / '.agents').exists()
