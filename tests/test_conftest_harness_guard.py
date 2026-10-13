"""Test collection disables live transport even with an empty inherited flag."""
import runpy
from pathlib import Path

import pytest

from litetui import harness


@pytest.mark.parametrize("inherited", [None, ""])
def test_collection_disables_live_transport(monkeypatch, inherited):
    if inherited is None:
        monkeypatch.delenv(harness.NO_HARNESS_ENV, raising=False)
    else:
        monkeypatch.setenv(harness.NO_HARNESS_ENV, inherited)
    runpy.run_path(str(Path(__file__).with_name("conftest.py")))
    assert harness.harness_disabled()
