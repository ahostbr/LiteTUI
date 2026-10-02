"""T0308 integration: local RPC shutdown retains owned cleanup and original failure."""
import asyncio

import pytest

from litetui import agent_launch_context, agent_ownership, hook_host, voice_backend
from litetui.app import LiteTUI
from test_agent_launch_context import AID, owned, registration_host


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['normal', 'exception', 'cancel'])
async def test_rpc_close_precedes_hooks_and_always_releases_owned_storage(owned, monkeypatch, outcome):
    app, calls = registration_host(owned, succeeds=True)
    app._release_owned_storage = lambda: LiteTUI._release_owned_storage(app)
    app._stop_footer_sampler = lambda: calls.append('footer')
    monkeypatch.setattr(voice_backend, 'stop', lambda: calls.append('voice'))
    monkeypatch.setattr(hook_host, 'leave_conversation', lambda _: calls.append('leave'))
    monkeypatch.setattr(hook_host, 'queue_lifecycle', lambda *_: calls.append('queue'))
    async def drain(_):
        calls.append('drain')
    monkeypatch.setattr(hook_host, 'drain_lifecycle', drain)
    class Rpc:
        async def close(self):
            calls.append('rpc-close')
            assert owned.authority.agent_id == AID
            if outcome == 'exception':
                raise RuntimeError('fixture RPC close failure')
            if outcome == 'cancel':
                raise asyncio.CancelledError('fixture RPC close cancellation')
    app._local_rpc = Rpc()
    def release():
        calls.append('conversation-release')
        assert owned.authority.agent_id == AID
    app.store.release = release
    if outcome == 'normal':
        await LiteTUI.on_unmount(app)
        assert calls == ['rpc-close', 'footer', 'voice', 'leave', 'queue', 'drain', 'conversation-release']
    else:
        failure = RuntimeError if outcome == 'exception' else asyncio.CancelledError
        with pytest.raises(failure, match='fixture RPC close'):
            await LiteTUI.on_unmount(app)
        assert calls == ['rpc-close', 'conversation-release']
    with pytest.raises(agent_ownership.OwnershipError):
        owned.authority
    with agent_launch_context.acquire(owned.store.data_root, 'QuietHelm') as recovered:
        assert recovered.authority.agent_id == AID
