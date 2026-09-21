"""Tests for generation-isolated module loading (``plugin_reload_generation``).

Fake-only: the "module" being reloaded is a tiny inline source string exec'd into
a fresh ``types.ModuleType`` -- no disk read of a real plugin, no engine/
process/model load. The load-bearing invariant these tests pin: a fresh
generation is a DISTINCT object that is NEVER registered in ``sys.modules`` and
NEVER touches the live module, so on any failure the live generation is intact
and the caller has no rollback to claim.
"""
import sys
import types

import pytest

from litetui.plugin_reload_generation import (
    Generation,
    load_fresh_generation,
)

_FAKE = "litetui.plugins._ws7_fake_gen"  # deliberately not a real module

SRC_OK = (
    "def alpha_handler():\n"
    "    return 'fresh-alpha'\n"
    "PLUGIN = 'the-manifest'\n"
)
SRC_RAISES = "x = 1\nraise RuntimeError('boom mid-body')\n"
SRC_NO_PLUGIN = "def alpha_handler():\n    return 'x'\n"


@pytest.fixture
def live_module():
    """A stand-in for the LIVE module currently in sys.modules, saved/restored."""
    live = types.ModuleType(_FAKE)
    live.PLUGIN = "live-manifest"
    saved = sys.modules.get(_FAKE)
    sys.modules[_FAKE] = live
    yield live
    if saved is None:
        sys.modules.pop(_FAKE, None)
    else:
        sys.modules[_FAKE] = saved


def test_fresh_generation_is_a_distinct_object():
    gen = load_fresh_generation(_FAKE, SRC_OK)
    assert isinstance(gen, Generation)
    assert isinstance(gen.module, types.ModuleType)
    assert gen.module_name == _FAKE
    assert gen.manifest == "the-manifest"
    # The fresh generation's handler is callable and returns the fresh value.
    assert gen.module.alpha_handler() == "fresh-alpha"


def test_fresh_generation_is_never_registered_in_sys_modules(live_module):
    gen = load_fresh_generation(_FAKE, SRC_OK)
    # The LIVE module remains the sys.modules entry; the fresh one is NOT there.
    assert sys.modules[_FAKE] is live_module
    assert gen.module is not live_module
    assert gen.module is not sys.modules[_FAKE]


def test_live_module_is_untouched_on_success(live_module):
    before = live_module.__dict__.copy()
    load_fresh_generation(_FAKE, SRC_OK)
    # The live module's namespace gained nothing from the fresh exec.
    assert live_module.__dict__ == before
    assert live_module.PLUGIN == "live-manifest"


def test_failure_mid_body_raises_and_live_is_intact(live_module):
    with pytest.raises(RuntimeError, match="boom mid-body"):
        load_fresh_generation(_FAKE, SRC_RAISES)
    # The live generation is byte-for-byte the original; the half-built fresh
    # module was discarded (never registered).
    assert sys.modules[_FAKE] is live_module
    assert live_module.PLUGIN == "live-manifest"
    assert not hasattr(live_module, "x")  # the fresh body's assignment never landed


def test_missing_plugin_manifest_is_refused_not_a_crash(live_module):
    with pytest.raises(ValueError, match="no PLUGIN manifest"):
        load_fresh_generation(_FAKE, SRC_NO_PLUGIN)
    assert sys.modules[_FAKE] is live_module
    assert live_module.PLUGIN == "live-manifest"


def test_compile_error_is_refused_and_live_is_intact(live_module):
    with pytest.raises(SyntaxError):
        load_fresh_generation(_FAKE, "def broken(:\n    pass\n")
    assert sys.modules[_FAKE] is live_module
    assert live_module.PLUGIN == "live-manifest"


def test_generation_tokens_are_unique_per_load():
    a = load_fresh_generation(_FAKE, SRC_OK)
    b = load_fresh_generation(_FAKE, SRC_OK)
    assert a.token and b.token
    assert a.token != b.token  # each generation has its own identity
    assert a.module is not b.module


def test_works_when_no_live_module_is_present():
    # A brand-new name with nothing in sys.modules: still a fresh, unregistered
    # module; the failure/absence of a live module does not matter.
    name = "litetui.plugins._ws7_fake_gen_absent"
    try:
        gen = load_fresh_generation(name, SRC_OK)
        assert gen.manifest == "the-manifest"
        assert name not in sys.modules  # never registered
    finally:
        sys.modules.pop(name, None)
