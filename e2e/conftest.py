"""The opt-in gate for live-service tests.

Everything under e2e/ talks to a real model — LM Studio or llama.cpp — and a
default `pytest` run must never start one. The project documents a no-service
test promise, and a live thinking smoke sat in the default suite anyway,
failing with "no ThinkingBlock appeared!" on every machine without a model
resident.

TWO INDEPENDENT GATES, on purpose:

  1. LOCATION — pyproject's `testpaths = ["tests"]` does not include e2e/, so
     the default command never collects anything here.
  2. THIS FILE — even when someone runs `pytest e2e/` directly, every test is
     skipped unless LITETUI_E2E=1 is set.

Either alone is one edit from being lost: a future `testpaths` change would
silently arm gate 1, and a copied marker without this conftest would arm gate
2. Requiring both means an accident has to happen twice.

🔴 THIS FILE MUST STAY REAL. The runner previously declared `SLOW: set[str] =
set()` with a comment promising "excluded by default; pass --all", and neither
the set nor the flag was ever read. A facility that exists and is never invoked
is worse than none: it stops the next reader from noticing the gap. If this
gate is ever bypassed, delete it rather than leave it as decoration.
"""

from __future__ import annotations

import os

import pytest

ENV_GATE = "LITETUI_E2E"


def pytest_collection_modifyitems(config, items):
    """Skip every collected item unless the operator opted in.

    Applied to all items rather than only `live`-marked ones: e2e/ exists to
    hold service-dependent checks, so an unmarked test here is far more likely
    to be a forgotten marker than a deliberately hermetic test.
    """
    if os.environ.get(ENV_GATE) == "1":
        return
    skip = pytest.mark.skip(
        reason=f"live-service test: set {ENV_GATE}=1 to run (needs a model serving)"
    )
    for item in items:
        item.add_marker(skip)
