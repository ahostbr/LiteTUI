"""Filesystem work for an owned home must not scale with its siblings.

Counts cover Python filesystem calls, not native/subprocess I/O, fsync latency, scheduling or network stalls.
Printed times are informational, not timing assertions; settings baseline/final residency stubs differ.
Profile uses its setter and thinking/settings use command dispatch, not complete live-input or timer benchmarks.
Own-home safety cases cover identity/reparse failures, not every invalid-schema, deletion or half-write case.
"""
import builtins
import io
import json
import os
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from uuid import UUID

import pytest

import litetui
from litetui.agent_ownership import AgentSession
from litetui.agent_store import SETTINGS_NAME, AgentStore


def test_import_is_from_this_worktree():
    assert Path(litetui.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])


def populate(root, count):
    root.mkdir()
    for number in range(1, count + 1):
        name = f"Alpha{number:03d}"
        home = root / ".agents" / name
        home.mkdir(parents=True)
        (home / SETTINGS_NAME).write_text(json.dumps({
            "schema_version": 1, "name": name, "agent_id": str(UUID(int=number)),
            "execution": {"backend": "lmstudio", "model": "fixture", "thinking_level": "off"},
        }), encoding="utf-8")
    return AgentStore(root)


@contextmanager
def filesystem_calls():
    """Count primitive calls, including pathlib's stat/open implementations."""
    counts = Counter()
    with pytest.MonkeyPatch.context() as patch:
        for module, name in ((os, "stat"), (os, "lstat"), (os, "scandir"),
                             (os, "listdir"), (os, "open"), (io, "open"), (builtins, "open")):
            original = getattr(module, name)

            def counted(*args, _original=original, _name=name, **kwargs):
                counts[_name] += 1
                return _original(*args, **kwargs)

            patch.setattr(module, name, counted)
        started = perf_counter()
        yield counts
        counts["seconds"] = perf_counter() - started


def assert_constant(label, results):
    totals = [sum(value for key, value in result.items() if key != "seconds") for result in results]
    print(f"{label}: homes=1 calls={totals[0]} seconds={results[0]['seconds']:.6f}; "
          f"homes=400 calls={totals[1]} seconds={results[1]['seconds']:.6f}", flush=True)
    assert totals[1] <= totals[0] + 32, f"{label}: 1 home={totals[0]}, 400 homes={totals[1]}"


@pytest.mark.parametrize("operation", ["authority", "require_owned", "refresh_name"])
def test_owned_lookup_cost(tmp_path, operation):
    from litetui.harness import Seat

    results = []
    for count in (1, 400):
        store = populate(tmp_path / f"root-{count}", count)
        with AgentSession.acquire_existing(store, name="Alpha001") as session:
            seat = Seat(agent_id=str(UUID(int=1)), name="Alpha001", model="fixture", agent_session=session)
            seat.registered = True  # Exercise the registered idle path without transport.
            with filesystem_calls() as calls:
                if operation == "authority":
                    assert session.authority.name == "Alpha001"
                elif operation == "require_owned":
                    session._require_owned()
                else:
                    assert seat.refresh_name() is False
            results.append(calls)
    assert_constant(operation, results)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["profile", "submit", "thinking", "settings"])
async def test_headless_action_cost(tmp_path, monkeypatch, operation):
    from litetui.app import LiteTUI, PromptInput
    from litetui.settings_screen import SettingsBody

    results = []
    for count in (1, 400):
        root = tmp_path / f"root-{count}"
        store = populate(root, count)
        with monkeypatch.context() as patch, AgentSession.acquire_existing(store, name="Alpha001") as session:
            patch.setenv("LITETUI_DATA_ROOT", str(root))
            patch.chdir(root)
            app = LiteTUI(agent_session=session)
            # Only external backend work is replaced. Admission, ownership,
            # persistence, command dispatch, and mounted widgets remain real.
            app._connect = lambda: None
            app._fetch_ctx_window = lambda: None
            app._apply_context_length = lambda **kwargs: None
            patch.setattr(app.backend, "loaded_models", list)
            streamed = []
            app._stream = lambda streamed=streamed: streamed.append(True)
            async with app.run_test(size=(120, 45)) as pilot:
                await pilot.pause()
                with filesystem_calls() as calls:
                    if operation == "profile":
                        assert app.set_tool_profile("autonomous")
                        assert app.set_tool_profile("strict")
                    elif operation == "submit":
                        app.query_one("#message-input", PromptInput).value = "test message"
                        await pilot.press("enter")
                    elif operation == "thinking":
                        app._handle_command("/think high")
                    else:
                        app._handle_command("/settings")
                    await pilot.pause()
                if operation == "profile":
                    assert app.chosen_tool_profile == "strict"
                elif operation == "submit":
                    assert streamed == [True]
                    assert any(message.get("content") == "test message" for message in app.conversation)
                    assert app.convo_path.is_file()
                elif operation == "thinking":
                    assert session.authority.thinking_level == "high"
                else:
                    assert app.screen.query_one(SettingsBody)
                results.append(calls)
    assert_constant(operation, results)


@pytest.mark.parametrize("target", ["home", "root", "settings", "marker", "conversations"])
def test_owned_lookup_rejects_reparse_paths(tmp_path, monkeypatch, target):
    from types import SimpleNamespace

    from litetui.agent_store import StoreError

    store = populate(tmp_path / "root", 1)
    home = store.root / "Alpha001"
    suspect = {"home": home, "root": store.root, "settings": home / SETTINGS_NAME,
               "marker": home / ".agent.initializing", "conversations": home / "conversations"}[target]
    original = Path.lstat

    def reparse(path, *args, **kwargs):
        if path == suspect:
            return SimpleNamespace(st_mode=0, st_file_attributes=0x400)
        return original(path, *args, **kwargs)

    with AgentSession.acquire_existing(store, name="Alpha001") as session:
        monkeypatch.setattr(Path, "lstat", reparse)
        with pytest.raises(StoreError, match="Linked storage paths are not supported"):
            session.conversation_directory(str(UUID(int=3)))


def test_owned_identity_is_revalidated_without_cache(tmp_path):
    from litetui.agent_ownership import OwnershipError
    from litetui.agent_store import StoreError

    store = populate(tmp_path / "root", 1)
    with AgentSession.acquire_existing(store, name="Alpha001") as session:
        path = store.root / "Alpha001" / SETTINGS_NAME
        settings = json.loads(path.read_text(encoding="utf-8"))
        settings["agent_id"] = str(UUID(int=2))
        path.write_text(json.dumps(settings), encoding="utf-8")
        with pytest.raises(StoreError, match="Owned agent authority changed; reopen explicitly"):
            _ = session.authority
        with pytest.raises(StoreError, match="Owned agent authority changed; reopen explicitly"):
            session.conversation_directory(str(UUID(int=3)))
    with pytest.raises(OwnershipError, match="Agent session is not owned by this process"):
        _ = session.authority


@pytest.mark.parametrize("damage", ["identity", "name", "malformed", "missing", "execution"])
def test_late_sibling_damage_is_checked_on_discovery_not_owned_reads(tmp_path, damage):
    from litetui.agent_store import StoreError

    store = populate(tmp_path / "root", 2)
    with AgentSession.acquire_existing(store, name="Alpha001") as session:
        path = store.root / "Alpha002" / SETTINGS_NAME
        settings = json.loads(path.read_text(encoding="utf-8"))
        if damage == "identity":
            settings["agent_id"] = str(UUID(int=1))
        elif damage == "name":
            settings["name"] = "Alpha001"
        elif damage == "execution":
            settings["execution"] = {}
        path.write_text("{" if damage == "malformed" else json.dumps(settings), encoding="utf-8")
        if damage == "missing":
            path.unlink()  # This test's own fixture.
        assert session.authority.name == "Alpha001"
        assert session.conversation_directory(str(UUID(int=3))).parent == store.root / "Alpha001" / "conversations"
        with pytest.raises(StoreError):
            store.find_agent(name="Alpha001")
        with pytest.raises(StoreError):
            store.find_agent(agent_id=str(UUID(int=1)))
        with pytest.raises(StoreError):
            AgentSession.acquire_existing(store, name="Alpha002")
