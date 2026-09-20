import os
import pytest


def test_owner_identity_reuse_and_unknown_fail_closed():
    from litetui.resource_identity import ResourceOwner, owner_alive
    owner = ResourceOwner('instance', 42, 'old')
    assert owner_alive(owner.encode(), probe=lambda pid: 'old', exists=lambda pid: True) is True
    assert owner_alive(owner.encode(), probe=lambda pid: 'new', exists=lambda pid: True) is False
    assert owner_alive(owner.encode(), probe=lambda pid: None, exists=lambda pid: True) is None
    assert owner_alive(owner.encode(), probe=lambda pid: None, exists=lambda pid: False) is False
    assert owner_alive('legacy-owner', probe=lambda pid: 'new', exists=lambda pid: False) is None


def test_current_owner_kernel_identity():
    from litetui.resource_identity import ResourceOwner, owner_alive
    owner = ResourceOwner.current('fixture')
    assert owner.pid == os.getpid()
    assert owner_alive(owner.encode()) is True


@pytest.mark.parametrize('raw', ['{}', '[]', 'null', '{"instance":"x","pid":true,"created":"1"}'])
def test_malformed_owner_is_unknown(raw):
    from litetui.resource_identity import owner_alive
    assert owner_alive(raw) is None
