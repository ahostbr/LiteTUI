import asyncio
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from litetui.model_resource_session import ModelResourceSession, AdmissionBlocked
from litetui.resource_admission import ModelDemand


def test_failed_reload_release_retains_claim_and_blocks_redispatch():
    demand = ModelDemand('local', 'endpoint', 'model', 10, {})
    coordinator = Mock()
    coordinator.reserve.return_value = SimpleNamespace(status='admitted', reservation_id='peak')
    coordinator.release.return_value = False
    session = ModelResourceSession(coordinator, 'owner', demand_for=lambda key: demand)
    session.leases['model'] = ('lease', demand)
    calls = []
    async def run():
        with pytest.raises(AdmissionBlocked, match='accounting'):
            async with session.load('model', reload=True):
                calls.append('first')
        assert session.unsettled_loads['model'] == 'peak'
        with pytest.raises(AdmissionBlocked, match='previous loader'):
            async with session.load('model', reload=True):
                calls.append('second')
    asyncio.run(run())
    assert calls == ['first']
