"""The shutdown guard sits where it has to, and says so if Textual moves. T723.

This file does NOT try to reproduce the race — that is
`tests/t723_shutdown_prune_probe.py`, which is an instrument (~3 whole-file runs
in 10 without the guard) and deliberately not collected. What is gated here is
the pair of facts the fix depends on, both of which are OUTSIDE our code and can
change under us:

  1. `App._shutdown` exists and is what we override.
  2. Inside it, the PRUNE happens before `Unmount` is dispatched.

Fact 2 is the entire reason the guard is on `_shutdown` rather than in
`on_unmount`. A wait in `on_unmount` was written first and measured to change
NOTHING, because by then the tree it waits for is already pruned. If a future
Textual moves the prune after the Unmount dispatch, the honest fix becomes the
ordinary handler and this override becomes pointless indirection — so that
reordering must go RED here, not pass quietly.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest
from textual.app import App

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m


def test_the_private_method_we_override_still_exists() -> None:
    """We override a private Textual method; a rename must be loud."""
    assert hasattr(App, "_shutdown"), (
        "textual.app.App._shutdown is gone; LiteTUI._shutdown overrides nothing and the "
        "T723 guard is now dead code — find the new seam before _close_all"
    )
    assert "_shutdown" in vars(m.LiteTUI), "LiteTUI no longer overrides _shutdown"


def test_the_prune_still_happens_before_unmount_is_dispatched() -> None:
    """The ordering that makes `on_unmount` useless for this, asserted.

    Read from Textual's own source rather than remembered: `_close_all` (the
    prune) must appear before the `events.Unmount()` dispatch inside
    `App._shutdown`.
    """
    source = inspect.getsource(App._shutdown)
    prune = source.find("_close_all")
    unmount = source.find("Unmount()")
    assert prune != -1, f"_close_all is gone from App._shutdown:\n{source}"
    assert unmount != -1, f"the Unmount dispatch is gone from App._shutdown:\n{source}"
    assert prune < unmount, (
        "Textual now dispatches Unmount BEFORE pruning. The T723 guard should move to "
        "on_unmount (an ordinary handler) instead of overriding a private method."
    )


@pytest.mark.asyncio
async def test_the_guard_runs_before_textual_tears_the_tree_down(monkeypatch) -> None:
    """The override actually calls the settle, and calls it BEFORE super().

    Without the ordering half, an implementation that settled after
    `super()._shutdown()` would satisfy "the guard is called" and do nothing at
    all — the tree is pruned by then.
    """
    order: list[str] = []
    original_settle = m.LiteTUI._settle_before_teardown
    original_shutdown = App._shutdown

    async def spy_settle(self):
        order.append("settle")
        await original_settle(self)

    async def spy_shutdown(self):
        order.append("textual-shutdown")
        await original_shutdown(self)

    monkeypatch.setattr(m.LiteTUI, "_settle_before_teardown", spy_settle)
    monkeypatch.setattr(App, "_shutdown", spy_shutdown)

    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    async with a.run_test(size=(80, 24)):
        pass

    assert order == ["settle", "textual-shutdown"], order


@pytest.mark.asyncio
async def test_the_control_switch_only_disables_it(monkeypatch) -> None:
    """`LITETUI_T723_GUARD=off` is the A/B control and nothing more.

    It exists so both arms of the measurement run identical code. An arm is
    kept on it because a switch that silently stopped working would make every
    future control run look like a fixed tree.
    """
    monkeypatch.setenv("LITETUI_T723_GUARD", "off")
    waited = []
    from litetui import side_panel

    async def spy(root):
        waited.append(root)

    monkeypatch.setattr(side_panel, "await_subtree_composed", spy)
    monkeypatch.setattr(m, "await_subtree_composed", spy)

    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    async with a.run_test(size=(80, 24)):
        pass
    assert waited == [], "the control switch did not disable the wait"

    monkeypatch.setenv("LITETUI_T723_GUARD", "on")
    b = m.LiteTUI()
    b.available_models = ["a-model"]
    b.model_id = "a-model"
    b._connect = lambda: None
    b._fetch_ctx_window = lambda: None
    async with b.run_test(size=(80, 24)):
        pass
    assert waited, "the wait did not run with the switch in its normal position"
