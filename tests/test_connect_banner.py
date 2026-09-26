"""The "using X" banner must name the model that is ACTUALLY answering you.

WHAT HAPPENED (D4). `_connect` printed

    Default model 'X' is not being served — using {self.available_models[0]!r}

*before* the pick had happened, and it named `available_models[0]` — a second,
parallel copy of a decision the pick logic makes differently. The pick prefers a
LOADED model (`loaded[0] if loaded else available_models[0]`), and it leaves an
already-valid `self.model_id` alone entirely. Whenever either of those applies,
the line the user reads is a lie about which model is serving them.

The negative control is the whole test. A fixture where `available_models[0]`
and the pick HAPPEN to be the same model cannot fail, and therefore proves
nothing — so every case below is built so the two DIFFER, and each asserts the
difference actually holds before asserting the banner.

Properties, each with a control:
  1. A cold model first in the listing, a warm one second: the pick is the warm
     one, so the banner must say the warm one.
  2. A model_id already chosen (mid-session /model, then a reconnect) survives
     the pick, so the banner must say IT — not the head of the listing.
  3. Control: when the head of the listing genuinely IS the pick, the banner
     still names it. Guards against "fixing" this by deleting the banner.
  4. Control: a default that IS being served prints no banner at all. Guards
     against a fix that makes the banner unconditional.
"""

from __future__ import annotations

import asyncio
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import llm_backend, paths, settings_runtime
from litetui.plugins.model_switch import switch_model
from litetui.settings import Settings
from litetui.settings_service import SettingsService

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-banner-"))


# ── fakes ──────────────────────────────────────────────────────────────────

class _FakeBackend:
    """Serves a fixed listing. No process, no socket, no catalog sync."""

    name = "lmstudio"

    def __init__(self, rows: list[llm_backend.ModelRow], name: str = "lmstudio"):
        self._rows = rows
        self.name = self.label = name

    def set_settings(self, settings) -> None:
        self.settings = settings

    def shutdown(self) -> None:
        pass

    def base_url(self) -> str:
        return "http://localhost:1234/v1"

    def host(self) -> str:
        return "localhost:1234"

    async def ensure_running(self) -> str:
        return "ok"

    async def list_models(self) -> list[llm_backend.ModelRow]:
        return list(self._rows)

    def request_overrides(self, key):
        return {}


def _rows(*specs: tuple[str, bool]) -> list[llm_backend.ModelRow]:
    return [
        llm_backend.ModelRow(key=k, path=None, source="server", loaded=loaded)
        for k, loaded in specs
    ]


def _app(
    rows, *, model_id: str = "", initial_model: str | None = None, **settings
) -> app_mod.LiteTUI:
    a = app_mod.LiteTUI(initial_model=initial_model)
    a.settings = Settings(**settings)
    a.backend = _FakeBackend(rows)
    a.model_id = model_id
    a.said: list[str] = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))
    a._update_header = lambda: None      # not part of the pick
    a._fetch_ctx_window = lambda: None   # would talk to a server
    return a


async def _connect(a, pilot) -> None:
    """Wait out the connect that `on_mount` already started.

    Deliberately does NOT call `_connect()` again: `on_mount` fires it at boot,
    and a second call in the same exclusive "init" group races the first —
    which showed up here as the banner being emitted twice. Boot is also the
    path a user actually takes.
    """
    for _ in range(40):
        await pilot.pause()
        await asyncio.sleep(0.01)
        if any("Connected — model:" in s or "Could not connect" in s
               or "No chat model available" in s for s in a.said):
            break


def _banner(a) -> str:
    hits = [s for s in a.said if "is not being served" in s]
    assert len(hits) == 1, f"expected exactly one 'not being served' banner, got {hits}"
    return hits[0]


# ── 1. the pick prefers a LOADED model; the banner must follow it ──────────

@pytest.mark.asyncio
async def test_banner_names_the_loaded_pick_not_the_head_of_the_listing():
    a = _app(
        _rows(("cold/first", False), ("warm/second", True)),
        default_model="ghost/not-served",
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)

    # The control: the two candidates must actually DIFFER, or this test is
    # incapable of failing and proves nothing.
    assert a.available_models[0] == "cold/first"
    assert a.model_id == "warm/second", (
        "precondition broken: the pick must prefer the loaded model"
    )

    b = _banner(a)
    assert "'warm/second'" in b, f"banner must name what is serving: {b!r}"
    assert "cold/first" not in b, f"banner named a model nobody picked: {b!r}"


# ── 2. an already-valid model_id survives the pick — the banner must too ───

@pytest.mark.asyncio
async def test_banner_names_a_surviving_model_id_not_the_head_of_the_listing():
    """A mid-session /model switch, then a reconnect. `pin_default_model` is
    off, so the pick block does not fire and `model_id` is untouched."""
    a = _app(
        _rows(("alpha/one", True), ("beta/two", True)),
        model_id="beta/two",
        default_model="ghost/not-served",
        pin_default_model=False,
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)

    assert a.available_models[0] == "alpha/one"
    assert a.model_id == "beta/two", (
        "precondition broken: a valid model_id must survive connect"
    )

    b = _banner(a)
    assert "'beta/two'" in b, f"banner must name what is serving: {b!r}"
    assert "alpha/one" not in b, f"banner named a model nobody picked: {b!r}"


# ── 3. control: when the head IS the pick, the banner still speaks ─────────

@pytest.mark.asyncio
async def test_banner_still_fires_when_the_head_happens_to_be_the_pick():
    """Deleting the banner would satisfy tests 1 and 2. It must not satisfy
    this one."""
    a = _app(
        _rows(("only/one", False)),
        default_model="ghost/not-served",
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)

    assert a.model_id == "only/one"
    b = _banner(a)
    assert "'ghost/not-served'" in b, f"banner must name the missing default: {b!r}"
    assert "'only/one'" in b, f"banner must name the pick: {b!r}"


# ── 4. control: a default that IS served prints nothing ────────────────────

@pytest.mark.asyncio
async def test_no_banner_when_the_default_is_being_served():
    a = _app(
        _rows(("cold/first", False), ("kept/default", True)),
        default_model="kept/default",
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)

    assert a.model_id == "kept/default"
    assert not [s for s in a.said if "is not being served" in s], (
        "a served default must print no banner at all"
    )


@pytest.mark.asyncio
async def test_explicit_launch_model_beats_pin_on_fresh_connect_and_banner():
    a = _app(
        _rows(("pinned/default", True), ("explicit/model", True)),
        initial_model="explicit/model",
        default_model="pinned/default",
        pin_default_model=True,
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)

    assert a.model_id == "explicit/model"
    assert "Connected — model: explicit/model" in a.said
    assert "Connected — model: pinned/default" not in a.said


@pytest.mark.asyncio
async def test_explicit_launch_model_survives_reconnect_with_pin():
    a = _app(
        _rows(("pinned/default", True), ("explicit/model", True)),
        initial_model="explicit/model",
        default_model="pinned/default",
        pin_default_model=True,
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)
        a.said.clear()
        a.connect()
        await _connect(a, pilot)

    assert a.model_id == "explicit/model"
    assert "Connected — model: explicit/model" in a.said


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param("backend-change", id="backend-change"),
        pytest.param("engine-toggle", id="engine-toggle"),
    ],
)
async def test_explicit_model_survives_connection_replacement(
    replacement, tmp_path, monkeypatch
):
    """Drive the real backend-change and codex-engine-toggle preparation."""
    rows = _rows(("pinned/default", True), ("explicit/model", True))
    a = _app(
        rows,
        initial_model="explicit/model",
        default_model="pinned/default",
        pin_default_model=True,
    )
    service = SettingsService(tmp_path)
    snapshot = service.create_conversation("abc")
    a._settings_service = service
    a.convo_dir = tmp_path / ".convos" / "abc"
    a.settings = deepcopy(snapshot.effective)
    a.settings.default_model = "pinned/default"
    a.settings.pin_default_model = True
    a.settings.backend = "lmstudio"
    a.settings.codex_native_engine = False

    target = deepcopy(a.settings)
    if replacement == "backend-change":
        target.backend = "codex"
    else:
        target.codex_native_engine = True
    monkeypatch.setattr(
        service,
        "snapshot",
        lambda _cid: SimpleNamespace(effective=deepcopy(target)),
    )
    monkeypatch.setattr(
        llm_backend,
        "make_backend",
        lambda settings: _FakeBackend(rows, name=settings.backend),
    )

    async with a.run_test() as pilot:
        await _connect(a, pilot)
        a.said.clear()
        settings_runtime.prepare_reconnect(a)
        assert a.model_id == "", "replacement must clear the old catalog selection"
        a.connect()
        await _connect(a, pilot)

    assert a.model_id == "explicit/model"
    assert "Connected — model: explicit/model" in a.said


@pytest.mark.asyncio
async def test_connect_never_persists_pin_over_explicit_launch_model():
    a = _app(
        _rows(("pinned/default", True), ("explicit/model", True)),
        initial_model="explicit/model",
        default_model="pinned/default",
        pin_default_model=True,
    )
    remembered = []
    a._remember_for_this_convo = lambda key, value: remembered.append((key, value))
    async with a.run_test() as pilot:
        await _connect(a, pilot)

    assert a.model_id == "explicit/model"
    assert ("model", "pinned/default") not in remembered
    assert ("model", "explicit/model") not in remembered


@pytest.mark.asyncio
@pytest.mark.parametrize("pin", [False, True])
async def test_user_model_pick_retires_launch_choice_before_reconnect(pin):
    rows = _rows(
        ("pinned/default", True),
        ("explicit/model", True),
        ("user/pick", True),
    )
    a = _app(
        rows,
        initial_model="explicit/model",
        default_model="pinned/default",
        pin_default_model=pin,
    )
    async with a.run_test() as pilot:
        await _connect(a, pilot)
        assert switch_model(a, "user/pick")
        a.said.clear()
        a.connect()
        await _connect(a, pilot)

    assert a._cli_initial_model is None
    assert a.model_id == "user/pick"
    assert "Connected — model: user/pick" in a.said


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param("backend-change", id="backend-change"),
        pytest.param("engine-toggle", id="engine-toggle"),
    ],
)
async def test_user_model_pick_survives_connection_replacement(
    replacement, tmp_path, monkeypatch
):
    rows = _rows(
        ("pinned/default", True),
        ("explicit/model", True),
        ("user/pick", True),
    )
    a = _app(
        rows,
        initial_model="explicit/model",
        default_model="pinned/default",
        pin_default_model=True,
    )
    service = SettingsService(tmp_path)
    snapshot = service.create_conversation("abc")
    a._settings_service = service
    a.convo_dir = tmp_path / ".convos" / "abc"
    a.settings = deepcopy(snapshot.effective)
    a.settings.default_model = "pinned/default"
    a.settings.pin_default_model = True
    a.settings.backend = "lmstudio"
    a.settings.codex_native_engine = False
    target = deepcopy(a.settings)
    if replacement == "backend-change":
        target.backend = "codex"
    else:
        target.codex_native_engine = True
    monkeypatch.setattr(
        service,
        "snapshot",
        lambda _cid: SimpleNamespace(effective=deepcopy(target)),
    )
    monkeypatch.setattr(
        llm_backend,
        "make_backend",
        lambda settings: _FakeBackend(rows, name=settings.backend),
    )

    async with a.run_test() as pilot:
        await _connect(a, pilot)
        assert switch_model(a, "user/pick")
        settings_runtime.prepare_reconnect(a)
        a.connect()
        await _connect(a, pilot)

    assert a.model_id == "user/pick"


def test_send_time_residency_does_not_restore_retired_launch_model():
    app = SimpleNamespace(
        _cli_initial_model="explicit/model",
        model_id="user/pick",
        backend=SimpleNamespace(
            loaded_models=lambda: ["explicit/model", "user/pick"],
        ),
        settings=Settings(),
    )
    app_mod.LiteTUI.retire_cli_model(app, "user/pick")

    action, model, _reason = app_mod.LiteTUI._headless_model_decision(app)

    assert action == "ok"
    assert model == "user/pick"
