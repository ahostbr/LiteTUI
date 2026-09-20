import pytest


def test_lmstudio_inventory_requires_explicit_residency():
    from litetui.resource_inventory import lmstudio_residency
    assert lmstudio_residency([{'id': 'm', 'state': 'loaded'}], 'm') is True
    assert lmstudio_residency([{'id': 'm', 'state': 'not-loaded'}], 'm') is False
    assert lmstudio_residency([{'id': 'm', 'loaded_context_length': 8192}], 'm') is True
    assert lmstudio_residency([{'id': 'm'}], 'm') is None
    assert lmstudio_residency([], 'm') is False


@pytest.mark.parametrize('rows', [None, {}, ['m'], [{'id': 'm', 'state': 'unknown'}], [{'id': 'm', 'loaded_context_length': False}]])
def test_malformed_inventory_never_proves_absence(rows):
    from litetui.resource_inventory import lmstudio_residency
    assert lmstudio_residency(rows, 'm') is None


def test_duplicate_conflicting_inventory_is_unknown():
    from litetui.resource_inventory import lmstudio_residency
    assert lmstudio_residency([{'id': 'm', 'state': 'loaded'}, {'id': 'm', 'state': 'not-loaded'}], 'm') is None
