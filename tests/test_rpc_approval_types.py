"""Malformed approval values must not resolve the waiting tool's permission."""
from types import SimpleNamespace
import pytest
from litetui import rpc, tool_approval


@pytest.mark.parametrize('field,value', [('allow', 'false'), ('allow', 0), ('allow', None),
    ('allow', []), ('remember', 'false'), ('remember', 1), ('remember', None)])
def test_invalid_approval_never_reaches_resolver(monkeypatch, field, value):
    called, responses = [], []
    monkeypatch.setattr(tool_approval, 'resolve_over_rpc', lambda *args: called.append(args) or True)
    monkeypatch.setattr(rpc, '_respond', lambda *args, **kwargs: responses.append(kwargs))
    cmd = {'type': 'approve', 'approval_id': 'pending', 'allow': True, field: value}
    rpc._dispatch(SimpleNamespace(), cmd)
    assert not called
    assert responses[-1]['ok'] is False


def test_explicit_deny_reaches_resolver_unchanged(monkeypatch):
    called = []
    monkeypatch.setattr(tool_approval, 'resolve_over_rpc', lambda *args: called.append(args) or True)
    monkeypatch.setattr(rpc, '_respond', lambda *args, **kwargs: None)
    rpc._dispatch(SimpleNamespace(), {'type': 'approve', 'approval_id': 'pending', 'allow': False})
    assert called[0][2:] == (False, False)


@pytest.mark.asyncio
async def test_malformed_wire_approval_cannot_release_waiting_tool(monkeypatch):
    import asyncio
    future = asyncio.get_running_loop().create_future()
    app = SimpleNamespace(_approval_waiters={'pending': future})
    executed = []
    async def waiting_tool():
        answer = await future
        if answer != tool_approval.DENIED:
            executed.append('tool ran')
    worker = asyncio.create_task(waiting_tool())
    monkeypatch.setattr(rpc, '_respond', lambda *args, **kwargs: None)
    try:
        rpc._dispatch(app, {'type': 'approve', 'approval_id': 'pending', 'allow': 'false'})
        await asyncio.sleep(0)
        assert not future.done()
        assert executed == []
        rpc._dispatch(app, {'type': 'approve', 'approval_id': 'stale', 'allow': True})
        assert not future.done()
        rpc._dispatch(app, {'type': 'approve', 'approval_id': 'pending', 'allow': False})
        await worker
        assert executed == []
    finally:
        if not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
