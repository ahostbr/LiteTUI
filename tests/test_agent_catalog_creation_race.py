"""A home another process is still creating is waited for, not read half-made (T1307).

AgentSession.create_fresh builds a home under the catalog lease: mkdir, the
INITIALIZING marker, `.settings.initializing.json`, the seed files, then the
`settings.json` link and the marker's removal. Readers (agent_launch_context
.ordinary's catalog read, find_agent, acquire) did not take that lease, so a
second process starting while the first was mid-creation read the sibling's
missing settings.json and failed closed ("Agent settings are absent or
unreadable"): a launch failed about one run in two when two agents were
created at once (Oxide 9b6773c9). agent_ownership.settled_catalog waits for
that creator; the store itself stays a strict, stdlib-only reader. These tests
hold the real catalog lease and leave a home in each of the creator's
in-between states, the way that creator does, then finish it from another thread.
"""
import json
import threading
import time
from uuid import uuid4

import pytest

from litetui.agent_launch_context import ordinary
from litetui.agent_ownership import CATALOG_LEASE_NAME, AgentSession, _KernelLease, settled_catalog
from litetui.agent_store import (
    INITIALIZING_NAME, SCHEMA_VERSION, SETTINGS_NAME, AgentStore, StoreError)

EXECUTION = {"backend": "lmstudio", "model": "fixture-model", "thinking_level": "default"}


def _settings(name):
    return {"schema_version": SCHEMA_VERSION, "name": name, "agent_id": str(uuid4()),
            "execution": dict(EXECUTION)}


def _half_made(store, name, *, marker):
    """A home as create_fresh leaves it before publication: the directory alone, or the
    blocking marker beside the initializing settings."""
    store.root.mkdir(exist_ok=True)
    directory = store.agent_directory(name)
    directory.mkdir()
    settings = _settings(name)
    if marker:
        (directory / INITIALIZING_NAME).write_text("Initialization incomplete; do not activate.\n",
                                                   encoding="utf-8")
        (directory / ".settings.initializing.json").write_text(json.dumps(settings), encoding="utf-8")
    return directory, settings


def _finish(directory, settings):
    (directory / SETTINGS_NAME).write_text(json.dumps(settings), encoding="utf-8")
    for leftover in (".settings.initializing.json", INITIALIZING_NAME):
        (directory / leftover).unlink(missing_ok=True)


def _creator_finishing(directory, settings, catalog, after=0.3):
    def finish():
        time.sleep(after)
        _finish(directory, settings)
        catalog.release()
    worker = threading.Thread(target=finish)
    worker.start()
    return worker


@pytest.mark.parametrize("marker", [False, True], ids=["bare-directory", "initializing-marker"])
def test_a_home_mid_creation_is_waited_for(tmp_path, marker):
    store = AgentStore(tmp_path)
    catalog = _KernelLease(tmp_path / CATALOG_LEASE_NAME, timeout=10)
    catalog.acquire()  # the creator's own lease, held across its whole creation
    directory, settings = _half_made(store, "Half", marker=marker)
    worker = _creator_finishing(directory, settings, catalog)
    try:
        agents = settled_catalog(store)
    finally:
        worker.join(10)
        catalog.release()
    assert [agent.name for agent in agents] == ["Half"]
    assert agents[0].agent_id == settings["agent_id"]


def test_an_abandoned_half_made_home_still_fails_closed(tmp_path):
    """Nobody holds the catalog lease: the creation was interrupted, and an interrupted home
    is never activated or repaired on read (create_fresh's own rule)."""
    store = AgentStore(tmp_path)
    _half_made(store, "Abandoned", marker=True)
    started = time.monotonic()
    with pytest.raises(StoreError, match="Agent settings are absent or unreadable"):
        settled_catalog(store)
    assert time.monotonic() - started < 5  # no wait for a creator that is gone


def test_a_launch_beside_a_home_mid_creation_opens_its_own_seat(tmp_path):
    """The reported failure, through the real launch path: ordinary() for the default seat
    while another process is still creating "Half"."""
    from litetui.settings import Settings
    store = AgentStore(tmp_path)
    catalog = _KernelLease(tmp_path / CATALOG_LEASE_NAME, timeout=10)
    catalog.acquire()
    directory, settings = _half_made(store, "Half", marker=True)
    worker = _creator_finishing(directory, settings, catalog)
    try:
        session = ordinary(tmp_path, Settings(), notice=lambda _text: None)
    finally:
        worker.join(10)
        catalog.release()
    try:
        assert session.authority.name == "LiteTUI"
        assert sorted(agent.name for agent in store.list_agents()) == ["Half", "LiteTUI"]
    finally:
        session.release()


def test_creating_beside_an_abandoned_home_refuses_without_waiting_on_itself(tmp_path):
    """create_fresh reads the catalog while it holds the catalog lease: it must not wait for
    itself, and an abandoned sibling still blocks it, as before."""
    store = AgentStore(tmp_path)
    _half_made(store, "Abandoned", marker=True)
    started = time.monotonic()
    with pytest.raises(StoreError, match="Agent settings are absent or unreadable"):
        AgentSession.create_fresh(store, name="Next", agent_id=str(uuid4()), **EXECUTION)
    assert time.monotonic() - started < 5
