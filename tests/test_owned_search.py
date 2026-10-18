"""Actual SQLite search/catalog path binding; all data under temp roots."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from litetui.agent_launch_context import create
from litetui.agent_store import StoreError

AID = '11111111-1111-4111-8111-111111111111'
CID = '33333333-3333-4333-8333-333333333333'


def engine(tmp_path):
    spec = importlib.util.spec_from_file_location('fixture_convo_search', Path(__file__).parents[1] / 'tools/convo_search.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._DATA_ROOT = tmp_path
    module.DB_PATH = str(tmp_path / 'search.db')
    return module


def transcript(directory, word):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'convo.jsonl'
    path.write_text(json.dumps({'type': 'meta', 'id': CID, 'created': 1}) + '\n' +
                    json.dumps({'type': 'msg', 'message': {'role': 'user', 'content': word}}) + '\n')
    return path


def test_legacy_read_and_owned_precedence_path_switch_equal_size(tmp_path, capsys):
    search = engine(tmp_path)
    legacy = transcript(tmp_path / '.convos' / CID, 'legacyword')
    search.build_index()
    before = legacy.read_bytes()
    with create(tmp_path, 'QuietHelm', agent_id=AID, backend='codex', model='fixture', thinking_level='high') as session:
        owned = transcript(session.conversation_directory(CID), 'owned-word')
        os.utime(owned, ns=(legacy.stat().st_atime_ns, legacy.stat().st_mtime_ns))
        assert owned.stat().st_size == legacy.stat().st_size
        search.build_index()
        with search.open_db() as conn:
            assert conn.execute('SELECT COUNT(*) FROM convos').fetchone()[0] == 1
            assert conn.execute('SELECT source_path FROM convos').fetchone()[0] == str(owned)
            with pytest.raises(StoreError, match='Refresh does not enable raw access'):
                search.do_raw(conn, CID, 'owned-word')
            assert conn.execute('SELECT text FROM messages').fetchone()[0] == 'owned-word'
    assert legacy.read_bytes() == before
    assert set(p.name for p in legacy.parent.iterdir()) == {'convo.jsonl'}


def test_corrupt_owned_catalog_does_not_create_index(tmp_path):
    search = engine(tmp_path)
    transcript(tmp_path / '.convos' / CID, 'legacyword')
    bad = tmp_path / '.agents' / 'Broken'
    bad.mkdir(parents=True)
    (bad / 'settings.json').write_text('{}')
    with pytest.raises(StoreError):
        search.build_index()
    assert not Path(search.DB_PATH).exists()


@pytest.mark.parametrize('verb', ['query', 'show', 'raw', 'stats', 'messages'])
def test_direct_cli_reads_snapshot_without_catalog_or_build(tmp_path, monkeypatch, capsys, verb):
    import sys
    search = engine(tmp_path)
    legacy = transcript(tmp_path / '.convos' / CID, 'legacyword')
    search.build_index()
    before = legacy.read_bytes()
    database_before = Path(search.DB_PATH).read_bytes()
    with create(tmp_path, 'QuietHelm', agent_id=AID, backend='codex', model='fixture', thinking_level='high') as session:
        owned = transcript(session.conversation_directory(CID), 'owned-word')
        os.utime(owned, ns=(legacy.stat().st_atime_ns, legacy.stat().st_mtime_ns))
        assert owned.stat().st_size == legacy.stat().st_size
        def forbidden(*args, **kwargs):
            pytest.fail('lookup must not build or enumerate current ownership')
        monkeypatch.setattr(search, 'catalog_paths', forbidden)
        monkeypatch.setattr(search, 'build_index', forbidden)
        monkeypatch.setattr(search, 'open_db', forbidden)
        args = {'query': ['legacyword'], 'show': ['--show', CID],
                'raw': ['--raw', CID, 'owned-word'], 'stats': ['--stats'],
                'messages': ['legacyword', '--messages']}[verb]
        monkeypatch.setattr(sys, 'argv', ['convo_search', *args])
        capsys.readouterr()
        assert search.main() == (1 if verb == 'raw' else 0)
        output = capsys.readouterr()
        assert output.out.startswith('Last successful refresh UTC: ')
        assert 'owned-word' not in output.out
        if verb == 'raw':
            assert 'Refresh does not enable raw access' in output.out
        elif verb != 'stats':
            assert 'legacyword' in output.out
            assert 'snapshot' in output.out
        assert not output.err
    assert Path(search.DB_PATH).read_bytes() == database_before
    assert legacy.read_bytes() == before
    assert {p.name for p in legacy.parent.iterdir()} == {'convo.jsonl'}


@pytest.mark.parametrize('args', [[], ['--raw', CID]])
def test_empty_or_invalid_cli_does_not_create_index(tmp_path, monkeypatch, capsys, args):
    import sys
    search = engine(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['convo_search', *args])
    if args:
        with pytest.raises(SystemExit) as error:
            search.main()
        assert error.value.code == 2
    else:
        search.main()
        assert 'usage:' in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
