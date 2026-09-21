"""Lifecycle close/reconcile for ModelResourceSession — real SQLite, fake backend.

No engine, no network. Every model row / reservation / unload_claim is a real
coordinator write, so the borrowed-peer-ownership corner and the recoverable
failed-unload claim are exercised against the actual accounting, not a mock.
"""
import time

import pytest

from litetui.model_resource_session import ModelResourceSession, AdmissionBlocked
from litetui.resource_admission import ResourceCoordinator, ResourceSnapshot, ModelDemand


def _coord(tmp_path, name, ram=100):
    return ResourceCoordinator(tmp_path / name,
                               telemetry=lambda: ResourceSnapshot(time.time(), ram, {}, True))


DEMAND = ModelDemand('cpu', 'endpoint', 'model', 80, {})
ALWAYS_QUIESCENT = lambda key: True


@pytest.mark.asyncio
async def test_closing_blocks_new_loads(tmp_path):
    c = _coord(tmp_path, 'closing.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND)
    report = await s.close(quiescent=ALWAYS_QUIESCENT, unload=lambda k: True)
    assert report == {}
    assert s._closing is True
    with pytest.raises(AdmissionBlocked):
        async with s.load('model'):
            pass


@pytest.mark.asyncio
async def test_begin_close_blocks_new_loads_but_retains_everything(tmp_path):
    c = _coord(tmp_path, 'begin.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass
    s.begin_close()                      # sync, no evidence, no release/unload
    s.begin_close()                      # idempotent
    assert s._closing is True
    assert 'model' in s.leases           # lease retained
    assert c.reserve(DEMAND, 'other').status == 'blocked'   # capacity NOT freed
    with pytest.raises(AdmissionBlocked):
        async with s.load('other'):
            pass


@pytest.mark.asyncio
async def test_in_flight_key_is_retained(tmp_path):
    c = _coord(tmp_path, 'inflight.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):          # inside the body: active + unsettled, no lease yet
        report = await s.close(quiescent=ALWAYS_QUIESCENT, unload=lambda k: True)
    assert report == {'model': 'in_flight'}
    # capacity was retained through the close — the in-flight loader still holds it
    assert 'model' in s.leases


@pytest.mark.asyncio
async def test_not_quiescent_retains_lease(tmp_path):
    c = _coord(tmp_path, 'notq.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass
    report = await s.close(quiescent=lambda k: False, unload=lambda k: True)
    assert report == {'model': 'not_quiescent'}
    assert 'model' in s.leases
    assert c.reserve(DEMAND, 'other').status == 'blocked'   # not released


@pytest.mark.asyncio
async def test_quiescence_error_is_structured_unknown(tmp_path):
    c = _coord(tmp_path, 'qerr.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass

    def boom(key):
        raise RuntimeError('cannot reach backend')

    report = await s.close(quiescent=boom, unload=lambda k: True)
    assert report == {'model': 'quiescence_unknown'}
    assert 'model' in s.leases                              # retained, not discarded


@pytest.mark.asyncio
async def test_owned_unload_success_frees_capacity(tmp_path):
    c = _coord(tmp_path, 'owned-ok.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass
    calls = []
    report = await s.close(quiescent=ALWAYS_QUIESCENT,
                           unload=lambda k: calls.append(k) or True)
    assert report == {'model': 'unloaded'}
    assert calls == ['model']
    assert not s.leases and not s.unload_claims
    assert c.reserve(DEMAND, 'other').status == 'admitted'  # capacity actually freed


@pytest.mark.asyncio
async def test_unload_without_explicit_true_retains_claim_then_recovers(tmp_path):
    c = _coord(tmp_path, 'owned-fail.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass
    # A normal/None return is NOT proof of unload — the claim must be retained.
    report = await s.close(quiescent=ALWAYS_QUIESCENT, unload=lambda k: None)
    assert report == {'model': 'unload_failed'}
    assert s.unload_claims.get('model') is not None         # recoverable
    assert 'model' not in s.leases                           # lease already dropped
    assert c.reserve(DEMAND, 'other').status == 'blocked'    # model still 'unloading'
    # Retry under fresh quiescence re-drives the OUTSTANDING claim (union report).
    report2 = await s.close(quiescent=ALWAYS_QUIESCENT, unload=lambda k: True)
    assert report2 == {'model': 'unloaded'}
    assert not s.unload_claims
    assert c.reserve(DEMAND, 'other').status == 'admitted'


@pytest.mark.asyncio
async def test_borrowed_release_never_unloads(tmp_path):
    c = _coord(tmp_path, 'borrowed.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND)   # borrowed by default
    async with s.load('model'):
        pass
    calls = []
    report = await s.close(quiescent=ALWAYS_QUIESCENT,
                           unload=lambda k: calls.append(k) or True)
    assert report == {'model': 'released'}
    assert calls == []                                       # borrowed NEVER unloads
    assert c.reserve(DEMAND, 'other').status == 'admitted'   # lease dropped, no unload


@pytest.mark.asyncio
async def test_owned_peer_release_leaves_shared_borrower_untouched(tmp_path):
    c = _coord(tmp_path, 'shared.sqlite', ram=1000)
    a = ModelResourceSession(c, 'a', demand_for=lambda k: DEMAND, owned=True)
    b = ModelResourceSession(c, 'b', demand_for=lambda k: DEMAND)   # borrowed peer
    async with a.load('model'):
        pass
    async with b.load('model'):
        pass
    calls = []
    report = await a.close(quiescent=ALWAYS_QUIESCENT,
                           unload=lambda k: calls.append(k) or True)
    assert report == {'model': 'released_shared'}             # b still holds it resident
    assert calls == []
    assert 'model' in b.leases                                # borrower untouched


@pytest.mark.asyncio
async def test_borrower_holding_owned_row_does_not_auto_unload(tmp_path):
    # The MODEL ROW is owned (by a), but the last live lease is b's BORROW. The
    # coordinator would grant unload permission off the row; the session contract
    # (b is not owned) must refuse to unload and retain the claim instead.
    c = _coord(tmp_path, 'owned-row-borrow.sqlite', ram=1000)
    a = ModelResourceSession(c, 'a', demand_for=lambda k: DEMAND, owned=True)
    b = ModelResourceSession(c, 'b', demand_for=lambda k: DEMAND)
    async with a.load('model'):
        pass
    async with b.load('model'):
        pass
    assert a.release('model') is False       # a drops its lease; b is now last, row still owned
    calls = []
    report = await b.close(quiescent=ALWAYS_QUIESCENT,
                           unload=lambda k: calls.append(k) or True)
    assert report == {'model': 'released_unowned_claim'}
    assert calls == []                       # NEVER auto-unload a borrowed model
    assert b.unload_claims.get('model') is not None   # claim retained, inspectable


@pytest.mark.asyncio
async def test_concurrent_close_serializes_no_duplicate_unload(tmp_path):
    import asyncio
    c = _coord(tmp_path, 'concurrent.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass
    calls = []

    async def slow_unload(key):
        await asyncio.sleep(0.02)
        calls.append(key)
        return True

    r1, r2 = await asyncio.gather(
        s.close(quiescent=ALWAYS_QUIESCENT, unload=slow_unload),
        s.close(quiescent=ALWAYS_QUIESCENT, unload=slow_unload),
    )
    assert calls == ['model']                # serialized: unloaded exactly once
    assert 'unloaded' in (r1.get('model'), r2.get('model'))
    assert not s.unload_claims


@pytest.mark.asyncio
async def test_cancelled_unload_retains_claim_and_propagates(tmp_path):
    import asyncio
    c = _coord(tmp_path, 'cancel.sqlite')
    s = ModelResourceSession(c, 'o', demand_for=lambda k: DEMAND, owned=True)
    async with s.load('model'):
        pass

    async def cancelling_unload(key):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await s.close(quiescent=ALWAYS_QUIESCENT, unload=cancelling_unload)
    assert s.unload_claims.get('model') is not None          # claim retained
    # recover: the permission still stands, a real unload confirms it, no duplicate load
    report = await s.close(quiescent=ALWAYS_QUIESCENT, unload=lambda k: True)
    assert report == {'model': 'unloaded'}
