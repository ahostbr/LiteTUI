"""Default owned startup, concurrent default, and explicit spawned refusal."""
from dataclasses import replace
import pytest
from litetui import settings
from litetui.agent_launch_context import ordinary, acquire
from litetui.agent_store import StoreError
from litetui.agent_ownership import OwnershipError


def config():
    return replace(settings.Settings(), backend='codex', default_model='fixture', thinking_level='high', seat_name='LiteTUI')


def test_default_creates_then_reopens_same_identity(tmp_path):
    with ordinary(tmp_path, config()) as first:
        identity = first.authority.agent_id
        assert first.authority.name == 'LiteTUI'
    with ordinary(tmp_path, config()) as reopened:
        assert reopened.authority.agent_id == identity
    assert not (tmp_path / '.convos').exists()


def test_busy_default_falls_back_unique_but_explicit_spawn_refuses(tmp_path):
    messages = []
    with ordinary(tmp_path, config()) as first:
        with ordinary(tmp_path, config(), notice=messages.append) as second:
            assert second.authority.name.startswith('LiteTUI-')
            assert second.authority.agent_id != first.authority.agent_id
        with pytest.raises(OwnershipError):
            ordinary(tmp_path, config(), spawned_identity=(first.authority.agent_id, 'LiteTUI', 'worker'))
    assert len(messages) == 1


def test_inactive_default_never_creates_alternate(tmp_path):
    home = tmp_path / '.agents' / 'LiteTUI'
    home.mkdir(parents=True)
    (home / '.agent.initializing').write_text('blocked')
    with pytest.raises(StoreError):
        ordinary(tmp_path, config())
    assert set(p.name for p in home.parent.iterdir()) == {'LiteTUI'}
