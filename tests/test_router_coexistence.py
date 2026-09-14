"""Two apps, one router, and nobody restarts anybody.

WHAT HAPPENED. LiteSuite moved its llama-server to port 7470 — the port
LiteTUI already defaults to. LiteTUI's attach order treats a ROUTER on its own
port as "plausibly our own crashed orphan, so keep it manageable", which is a
good rule for the world it was written in: LiteTUI was the only app that
spawned routers there, so an unrecognised one really was its own. It is a
catastrophic rule for the world we are in now, where it means LiteTUI
regenerates its ini and restarts LiteSuite's server out from under the GUI.

`~/.litesuite/llm/router.json` is the fact that ends the guessing. These tests
are about the decision it feeds, not about the file (that is
`test_router_record.py`).

🔴 THE CONTROLS CARRY THE WEIGHT HERE. "Attached" is the safe answer, so a
change that simply attached to EVERYTHING would pass the headline test and
lock users out of their own router. Every case below is paired with the
opposite fixture — our own record, a dead pid, no record at all — that must
still come back manageable.
"""

from __future__ import annotations

import os

import pytest

from litetui import llm_backend, router_record
from litetui.settings import Settings

HOST = "http://127.0.0.1:7470"


@pytest.fixture(autouse=True)
def _record_in_a_tmp_home(tmp_path, monkeypatch):
    """No test here may read or write the developer's real router.json — it
    describes a router that is actually running on this machine."""
    path = tmp_path / "router.json"
    monkeypatch.setattr(router_record, "record_path", lambda: path)
    return path


def _backend(monkeypatch, *, shape=llm_backend._SHAPE_ROUTER):
    s = Settings()
    s.llama_host = HOST
    backend = llm_backend.LlamaCppBackend(s)
    # Our port answers, and nothing we spawned is running: the exact situation
    # where the attach order has to make a judgement call.
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: host.rstrip("/") == HOST)
    monkeypatch.setattr(backend, "_probe_shape", lambda: shape)
    return backend


def _write(path, *, owner, pid, port=7470):
    router_record.write(pid=pid, port=port, ini="theirs.ini", owner=owner, path=path)


def test_a_live_foreign_owner_on_our_port_is_ATTACHED(_record_in_a_tmp_home, monkeypatch):
    # The headline case. Before the record existed this returned "ok" and left
    # the router manageable, so the next load would regen the ini and restart
    # LiteSuite's process.
    _write(_record_in_a_tmp_home, owner="litesuite", pid=os.getpid())
    backend = _backend(monkeypatch)
    status = backend._ensure_running_sync()
    assert backend.attached is True
    assert backend.attached_owner == "litesuite"
    assert status == f"attached {HOST} (owner: litesuite)"


def test_live_same_family_record_is_a_sibling_not_an_orphan(
    _record_in_a_tmp_home, monkeypatch
):
    # An app-family owner is not proof of this INSTANCE owning a server.
    # A live legacy record cannot safely be claimed as a crashed orphan.
    _write(_record_in_a_tmp_home, owner="litetui", pid=os.getpid())
    backend = _backend(monkeypatch)
    assert "attached" in backend._ensure_running_sync()
    assert backend._attached_owner == "litetui"


def test_CONTROL_a_dead_owner_is_stale_and_ignored(_record_in_a_tmp_home, monkeypatch):
    # The record outlives the process that wrote it — a hard kill leaves one
    # behind. A stale claim must not hand our port to a ghost.
    _write(_record_in_a_tmp_home, owner="litesuite", pid=0x7FFFFFFF)
    backend = _backend(monkeypatch)
    assert backend._ensure_running_sync() == "ok"
    assert backend.attached is False


def test_CONTROL_no_record_at_all_keeps_the_old_behaviour(monkeypatch):
    # A router started by hand writes no record, and `owner` has no "external"
    # value precisely so that ABSENCE carries this meaning.
    backend = _backend(monkeypatch)
    assert backend._ensure_running_sync() == "ok"
    assert backend.attached is False


def test_CONTROL_a_record_about_a_DIFFERENT_port_says_nothing_about_ours(
    _record_in_a_tmp_home, monkeypatch
):
    # Ownership is per-port. A live LiteSuite router on 8088 is not a claim on
    # 7470, and treating it as one would attach us to a server that is not
    # even the one we are talking to.
    _write(_record_in_a_tmp_home, owner="litesuite", pid=os.getpid(), port=8088)
    backend = _backend(monkeypatch)
    assert backend._ensure_running_sync() == "ok"
    assert backend.attached is False


def test_CONTROL_our_OWN_dead_record_is_stale_TOO_and_leaves_the_router_manageable(
    _record_in_a_tmp_home, monkeypatch
):
    """⬜ T217 — THE ONE CASE THE FIVE CONTROLS ABOVE DID NOT COVER.

    They pin live-foreign (attach), live-own (manageable), DEAD-FOREIGN
    (ignored), no record, and another port. An OWN record naming a DEAD pid
    was the hole: it is what a hard-killed LiteTUI leaves behind, and it is
    the case the T217 reader change is about — liveness is now decided first
    and a dead record is discarded outright rather than surviving as a weaker
    claim for later code to re-check.

    ⚠️ THIS IS A PIN, NOT A KILL. It passed before the change as well, because
    the old conjunction reached the same verdict by a different route
    (`not is_mine` was already false for our own record). Reordering a
    conjunction cannot change its value, so nothing here can distinguish the
    two implementations — what this arm defends is the OUTCOME against a future
    edit that reads `record` after the guard and forgets that a dead one is not
    a claim.
    """
    _write(_record_in_a_tmp_home, owner="litetui", pid=0x7FFFFFFF)
    backend = _backend(monkeypatch)

    assert backend._ensure_running_sync() == "ok"
    assert backend.attached is False
    assert backend.attached_owner is None


def test_a_foreign_SINGLE_model_server_still_attaches_without_a_record(monkeypatch):
    # Unchanged behaviour, asserted so the new branch cannot swallow it: we
    # only ever spawn routers, so a single-model server here is definitively
    # not ours.
    backend = _backend(monkeypatch, shape=llm_backend._SHAPE_SINGLE)
    assert backend._ensure_running_sync() == "ok"
    assert backend.attached is True
    assert backend.attached_owner is None


def test_an_adopted_router_ALLOWS_a_load_but_REFUSES_the_ini(
    _record_in_a_tmp_home, monkeypatch
):
    """The two halves of coexistence, and they must not be confused.

    Hot-loading a model the shared router already has in its preset is what
    the route is FOR, and both apps see the result. Rewriting the ini is a
    claim on a file that belongs to whoever spawned the process, and doing it
    also restarts that process.
    """
    _write(_record_in_a_tmp_home, owner="litesuite", pid=os.getpid())
    backend = _backend(monkeypatch)
    backend._ensure_running_sync()

    backend._refuse_if_attached("load a model")          # must NOT raise

    with pytest.raises(llm_backend.BackendError) as excinfo:
        backend._regen_ini()
    message = str(excinfo.value)
    assert "LiteSuite" in message                        # named from the record
    assert "preset" in message


def test_a_single_model_server_refuses_a_load_and_says_why(monkeypatch):
    # The control for the branch above: a single-model server has no load
    # route at all, so "load refused — File Not Found" would blame the model
    # for a property of the server.
    backend = _backend(monkeypatch, shape=llm_backend._SHAPE_SINGLE)
    backend._ensure_running_sync()
    with pytest.raises(llm_backend.BackendError) as excinfo:
        backend._refuse_if_attached("load a model")
    assert "serving one model" in str(excinfo.value)


def test_an_unowned_foreign_server_is_not_blamed_on_LiteSuite(monkeypatch):
    """🔴 A REFUSAL THAT NAMES THE WRONG APP SENDS THE USER TO THE WRONG
    WINDOW. The old text said "LiteSuite owns the server" unconditionally —
    a guess that was right often enough to survive, and wrong for every
    hand-started llama-server.
    """
    backend = _backend(monkeypatch, shape=llm_backend._SHAPE_SINGLE)
    backend._ensure_running_sync()
    backend._attached_owner = None
    with pytest.raises(llm_backend.BackendError) as excinfo:
        backend._regen_ini()
    assert "another app" in str(excinfo.value)
    assert "LiteSuite" not in str(excinfo.value)
