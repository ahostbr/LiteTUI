"""Readonly owned/archive catalogs; legacy use itself is not frozen."""
import json

import pytest

from litetui import storage_catalog
from litetui.agent_launch_context import acquire
from litetui.agent_store import StoreError

AID = '11111111-1111-4111-8111-111111111111'
CID = '33333333-3333-4333-8333-333333333333'


def agent(root, name='QuietHelm', identity=AID):
    directory = root / '.agents' / name
    directory.mkdir(parents=True)
    (directory / 'settings.json').write_text(json.dumps({'schema_version': 1, 'name': name,
        'agent_id': identity, 'execution': {'backend': 'codex', 'model': 'fixture', 'thinking_level': 'high'}}))
    transcript = directory / 'conversations' / CID / 'convo.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b'owned transcript')
    return transcript


def test_readonly_catalog_owned_and_archive_same_uuid_are_distinct(tmp_path):
    owned = agent(tmp_path)
    archive = tmp_path / '.convos' / CID / 'convo.jsonl'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'legacy retained transcript')
    before = {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    rows = storage_catalog.conversations(tmp_path, include_archives=True)
    assert len(rows) == 2 and {r.archive for r in rows} == {False, True}
    selected = storage_catalog.locate_owned(tmp_path, CID, agent_id=AID)
    assert selected.transcript == owned and not selected.archive
    assert {p.relative_to(tmp_path).as_posix(): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert not list(tmp_path.rglob('*.lease'))


def test_owned_resolver_never_falls_back_to_matching_archive(tmp_path):
    archive = tmp_path / '.convos' / CID / 'convo.jsonl'
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b'archive')
    with pytest.raises(StoreError, match='absent'):
        storage_catalog.locate_owned(tmp_path, CID)
    assert not (tmp_path / '.agents').exists()
    assert storage_catalog.conversations(tmp_path, include_archives=True)[0].archive


def test_selected_owned_path_confines_and_does_not_create_missing_convo(tmp_path):
    agent(tmp_path)
    with acquire(tmp_path, 'QuietHelm') as session:
        missing = '44444444-4444-4444-8444-444444444444'
        path = storage_catalog.owned_transcript(session, missing)
        assert path == tmp_path / '.agents' / 'QuietHelm' / 'conversations' / missing / 'convo.jsonl'
        assert not path.parent.exists()
        with pytest.raises(StoreError):
            storage_catalog.owned_transcript(session, '../escape')


def test_ambiguous_owned_uuid_requires_explicit_agent(tmp_path):
    agent(tmp_path)
    other = '44444444-4444-4444-8444-444444444444'
    agent(tmp_path, 'OtherName', other)
    with pytest.raises(StoreError, match='ambiguous'):
        storage_catalog.locate_owned(tmp_path, CID)
    assert storage_catalog.locate_owned(tmp_path, CID, agent_id=other).agent_name == 'OtherName'
