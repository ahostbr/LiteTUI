"""T0306-O — the built-in fleet floor is the published one: gpt-6.1-sol.

The release review found that with the shared policy file missing or malformed,
a seat fell back to a built-in default that still admitted gpt-6-sol/high, the
model liteharness's own built-in default refuses. A seat could /model to it
after a governed launch, have its next turn admitted, and then be refused by
the harness at its next resume.

How it regressed: 0.25.0 was published from ffad9c2 with fleet_policy.py equal
to liteharness's; merge 20b9174 took that release into main keeping main's own
tree, and main's copy was the older one.

conftest points LITESUITE_FLEET_POLICY at an absent file: the built-in default.
Every policy file here is written under tmp_path. No seat or process is launched.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from litetui import convo_settings as cs_mod
from litetui import fleet_policy, seat_authority
from litetui import settings as st

from test_convo_settings import _App  # noqa: E402
from test_fleet_floor_t1043 import _Seat, _typed  # noqa: E402

# Pinned, not read from the code under test: the built-in default liteharness ships.
PUBLISHED_MODELS = ["gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
                    "gpt-6-luna", "gpt-6.1-sol", "gpt-6-astra"]
PUBLISHED_FLOOR = "gpt-6.1-sol"
OLD_FLOOR = "gpt-6-sol"          # what the regressed default still admitted
LEVELS = ["off", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]


@pytest.fixture(params=["absent", "{not json", "[]"])
def fallback(request, tmp_path, monkeypatch):
    """The states in which a seat enforces the built-in default: no policy file,
    an unreadable one, and one that parses but is not a policy."""
    path = tmp_path / "fleet-policy.json"
    if request.param != "absent":
        path.write_text(request.param, encoding="utf-8")
    monkeypatch.setenv(fleet_policy.POLICY_ENV, str(path))
    return request.param


def _shared_policy(tmp_path, monkeypatch, models, min_model):
    path = tmp_path / "fleet-policy.json"
    path.write_text(json.dumps({"version": 1, "floors": {"codex": {
        "match_prefixes": ["gpt-"], "exempt_prefixes": ["gpt-oss-"],
        "models": models, "min_model": min_model,
        "thinking_levels": LEVELS, "min_thinking_level": "high"}}}), encoding="utf-8")
    monkeypatch.setenv(fleet_policy.POLICY_ENV, str(path))
    return path


def _refused(seat):
    return seat.streams == 0 and bool(seat.said) and "TURN REFUSED" in seat.said[-1]


def _ran_quietly(seat):
    return seat.streams == 1 and not any("TURN REFUSED" in t for t in seat.said)


def test_the_modules_under_test_are_this_checkouts_own():
    src = Path(__file__).resolve().parents[1] / "src" / "litetui"
    assert Path(fleet_policy.__file__).resolve() == src / "fleet_policy.py"
    assert Path(seat_authority.__file__).resolve() == src / "seat_authority.py"


# ── the built-in default ─────────────────────────────────────────────────────

def test_the_built_in_default_is_the_published_floor():
    floor = fleet_policy.DEFAULT_POLICY["floors"]["codex"]
    assert floor["min_model"] == PUBLISHED_FLOOR
    assert floor["models"] == PUBLISHED_MODELS


def test_the_pre_launch_gate_and_the_seat_both_refuse_the_old_floor_model():
    """fleet_policy.check is the gate a spawn or a resume runs before launch; it
    and the per-turn check must not disagree about one model."""
    spawn = fleet_policy.check("codex", OLD_FLOOR, "high")
    turn = seat_authority.floor_refusal(_Seat(model=OLD_FLOOR, thinking="high"), "harness")
    assert spawn is not None and spawn.startswith("SPAWN REFUSED"), spawn
    assert turn is not None and turn.startswith("TURN REFUSED"), turn


# ── no policy file, and a malformed one ──────────────────────────────────────

def test_FALLBACK_a_turn_on_the_old_floor_model_is_REFUSED(fallback):
    seat = _Seat(model=OLD_FLOOR, thinking="high")
    _typed(seat)
    assert _refused(seat) and seat.conversation == [], (seat.streams, seat.said)
    assert "built-in default" in seat.said[-1]
    assert seat.emitted[-1]["stopReason"] == "fleet_floor"


@pytest.mark.parametrize("model", [PUBLISHED_FLOOR, "gpt-6-astra"])
def test_FALLBACK_CONTROL_a_turn_at_or_above_the_published_floor_runs(fallback, model):
    seat = _Seat(model=model, thinking="high")
    _typed(seat)
    assert _ran_quietly(seat), (seat.streams, seat.said)


def _steer(seat):
    from litetui.codex_steering import HostSteering

    seat._stop_requested = False
    sent = []

    async def request(method, params):
        sent.append(method)
        return {"turnId": "turn"}

    steering = HostSteering(seat, SimpleNamespace(request=request), "thread", "turn", {}, lambda: None)
    entry = steering.ledger.enqueue(
        {"content": "steer me", "source": "queued", "tool_profile": "interactive"}, "thread", "turn")
    return steering, entry, request, sent


@pytest.mark.asyncio
async def test_FALLBACK_a_STEERED_item_on_the_old_floor_model_is_denied_and_never_sent(fallback):
    steering, entry, request, sent = _steer(_Seat(model=OLD_FLOOR, thinking="high"))
    state = await steering.ledger.deliver(entry, admit=steering.admit, request=request)
    assert state == "denied" and sent == [], (state, sent)
    assert entry["admission"]["reason"].startswith("TURN REFUSED")


@pytest.mark.asyncio
async def test_FALLBACK_CONTROL_a_STEERED_item_at_the_published_floor_is_sent(fallback):
    steering, entry, request, sent = _steer(_Seat(model=PUBLISHED_FLOOR, thinking="high"))
    state = await steering.ledger.deliver(entry, admit=steering.admit, request=request)
    assert state == "accepted" and sent == ["turn/steer"], (state, sent)


def test_FALLBACK_a_model_switch_to_the_old_floor_model_REFUSES_the_next_turn(fallback):
    seat = _Seat(model=PUBLISHED_FLOOR, thinking="high")
    _typed(seat)
    assert seat.streams == 1, "CONTROL: the governed launch runs"
    seat.model_id = OLD_FLOOR                # /model after launch
    _typed(seat, "mail", source="harness")
    assert seat.streams == 1 and "TURN REFUSED" in seat.said[-1], (seat.streams, seat.said)


# ── the switch, then a resume: the real setter, agent home and adoption ───────

@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    """An owned agent home launched on codex at the published floor."""
    from litetui.agent_launch_context import ordinary

    for key in ("LITETUI_BACKEND", "LITETUI_MODEL", "LITETUI_THINKING"):
        monkeypatch.delenv(key, raising=False)
    session = ordinary(tmp_path, st.Settings(
        backend="codex", default_model=PUBLISHED_FLOOR, thinking_level="high"))
    try:
        yield session
    finally:
        session.release()


def _host(session, convo, *, spawned):
    from litetui.agent_launch_context import apply_settings

    settings = st.Settings()
    apply_settings(session, settings)
    host = _App(settings, convo, backend_name="codex", owned_session=session)
    host._spawned_marker = spawned
    return host


def _launched(session):
    """A spawned seat's new conversation, as connect leaves it."""
    convo = session.conversation_directory(str(uuid4()))
    convo.mkdir()
    seat = _host(session, convo, spawned=True)
    seat._adopt_convo_settings(born=True)
    seat._model_id, seat._thinking_level = session.authority.model, session.authority.thinking_level
    assert seat_authority.floor_refusal(seat, "harness") is None, "CONTROL: the governed launch runs"
    return seat, convo


def _resumed(session, convo):
    """The same conversation reopened by a process without the spawn marker."""
    seat = _host(session, convo, spawned=False)
    seat._adopt_convo_settings(born=False)
    assert seat._spawned_seat is True, "a conversation born spawned stayed a fleet seat"
    return seat


def test_FALLBACK_a_switch_to_the_old_floor_model_is_still_REFUSED_after_a_RESUME(fallback, codex_home):
    seat, convo = _launched(codex_home)
    seat.model_id = OLD_FLOOR                # /model: the setter publishes it to the agent home
    assert codex_home.authority.model == OLD_FLOOR and cs_mod.load(convo).model == OLD_FLOOR

    resumed = _resumed(codex_home, convo)
    assert (resumed.backend.name, resumed.model_id, resumed.thinking_level) == ("codex", OLD_FLOOR, "high")
    why = seat_authority.floor_refusal(resumed, "harness")
    assert why is not None and why.startswith("TURN REFUSED"), why


def test_FALLBACK_CONTROL_a_resume_at_the_published_floor_runs(fallback, codex_home):
    _, convo = _launched(codex_home)
    resumed = _resumed(codex_home, convo)
    assert (resumed.backend.name, resumed.model_id, resumed.thinking_level) == ("codex", PUBLISHED_FLOOR, "high")
    assert seat_authority.floor_refusal(resumed, "harness") is None


# ── a valid shared policy is the authority; the default is only the fallback ──

def test_VALID_CONTROL_a_shared_policy_listing_the_old_floor_model_still_admits_it(tmp_path, monkeypatch):
    path = _shared_policy(tmp_path, monkeypatch, [
        "gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol",
        "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"], OLD_FLOOR)
    seat = _Seat(model=OLD_FLOOR, thinking="high")
    _typed(seat)
    assert seat.streams == 1 and seat.said == [], seat.said
    below = _Seat(model="gpt-5.6-sol", thinking="high")
    _typed(below)
    assert _refused(below) and str(path) in below.said[-1], below.said


def test_VALID_CONTROL_a_shared_policy_at_the_published_floor_is_the_one_that_refuses(tmp_path, monkeypatch):
    path = _shared_policy(tmp_path, monkeypatch, PUBLISHED_MODELS, PUBLISHED_FLOOR)
    seat = _Seat(model=OLD_FLOOR, thinking="high")
    _typed(seat)
    assert _refused(seat) and str(path) in seat.said[-1], seat.said
    assert "built-in default" not in seat.said[-1]
    at_floor = _Seat(model=PUBLISHED_FLOOR, thinking="high")
    _typed(at_floor)
    assert at_floor.streams == 1 and at_floor.said == [], at_floor.said


# ── the owner's own instance: the turns the owner drives stay exempt ──────────

def _owner():
    seat = _Seat(model=OLD_FLOOR, thinking="high")
    seat._spawned_seat, seat._owner_seat = False, True
    return seat


@pytest.mark.parametrize("source", sorted(seat_authority.ATTENDED_SOURCES))
def test_OWNER_CONTROL_an_attended_turn_goes_through_with_no_text_at_all(fallback, source):
    seat = _owner()
    _typed(seat, source=source)
    assert seat.streams == 1 and seat.said == [], (source, seat.said)


def test_OWNER_CONTROL_a_hellod_gui_rpc_turn_goes_through_with_no_text_at_all(fallback):
    seat = _owner()
    seat._gui_rpc_enabled = True
    _typed(seat, "from the composer", source="rpc")
    assert seat.streams == 1 and seat.said == [], seat.said


@pytest.mark.parametrize("source", ["harness", "scheduled", "child-result"])
def test_OWNER_an_UNATTENDED_turn_on_the_old_floor_model_is_REFUSED(fallback, source):
    seat = _owner()
    _typed(seat, "mail", source=source)
    assert _refused(seat), (source, seat.streams, seat.said)
