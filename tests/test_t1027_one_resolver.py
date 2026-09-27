"""T1027 — one resolver for a seat's authority, and the identity it was launched with.

Card T1027 (Marquee 27bec769, rulings 2b9aed25 + 4b9e7635). What it asks:
  1. ONE answer to "under what authority does this turn run", for typed, inbox,
     cron, goal and rpc turns alike. An explicit launch flag outranks the file for
     EVERY source (a ceiling); inbox/child mail narrows and never widens.
  2. A flag-launched NEW conversation is born with the flag's backend/model/
     thinking (identity), and with the flag's profile only when it is STRICTER
     (a restriction is not a grant, Sentinel 0118549d). A resume never switches
     backend silently.
  3. A seat whose name is held by a LIVE agent becomes <name>-2, never shares or
     steals it; a dead corpse's name is reclaimed.
  4. Presence carries the resolved backend and thinking level.

Every arm uses a temp directory; the registry arms run the REAL liteharness CLI
against a throwaway HOME and assert that before registering anything.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import convo_settings as cs_mod
from litetui import harness as harness_mod
from litetui import hook_host
from litetui import settings as st
from litetui import tool_policy


@pytest.fixture(autouse=True)
def _no_invocation_environment(monkeypatch):
    # conftest supplies an LM Studio boot default; these arms characterize
    # launch FLAGS, so the environment must not be a second invocation.
    for key in ("LITETUI_BACKEND", "LITETUI_MODEL", "LITETUI_THINKING"):
        monkeypatch.delenv(key, raising=False)


# ── 1. the turn's authority ──────────────────────────────────────────────────


class _Host:
    """Just enough app for `hook_host.accept_prompt` and the real resolver."""

    from litetui.app import LiteTUI as _Real

    chosen_tool_profile = _Real.chosen_tool_profile
    _remember_for_this_convo = _Real._remember_for_this_convo

    def __init__(self, saved_profile: str, flag: str | None, convo_dir: Path):
        self.settings = st.Settings()
        self.settings.tool_policy_profile = saved_profile
        cs_mod.save(convo_dir, cs_mod.ConvoSettings(tool_policy_profile=saved_profile))
        self._convo_settings = cs_mod.load(convo_dir)
        self._cli_tool_profile = flag
        self._active_tool_profile = flag or saved_profile
        self.backend = SimpleNamespace(name="codex", owns_native_turns=False)
        # At the T1043 fleet floor, so these arms test authority, not the floor.
        self.model_id, self._thinking_level = "gpt-6-sol", "high"
        self.appended: list[dict] = []

    def _append(self, message):
        self.appended.append(message)


def _accept(host, source, requested, **extra):
    item = {"content": "x", "tool_profile": requested, "source": source, **extra}
    hook_host.accept_prompt(host, item)
    return host._active_tool_profile


def test_the_FLAG_holds_for_a_typed_turn_and_then_an_inbox_turn(tmp_path: Path) -> None:
    """🔴 THE ROOT (design a9f22905). accept_prompt re-stamps the authority from
    what the producer carried, and no producer sees the flag: typed carries
    chosen_tool_profile (app.py:7207), inbox carries settings (app.py:2320). So
    the flag died at the FIRST accepted prompt, typed or not."""
    host = _Host("autonomous", "interactive", tmp_path)

    assert _accept(host, "typed", host.chosen_tool_profile) == "interactive", (
        "a typed turn ran the saved profile over --tool-profile")
    assert _accept(host, "harness", host.settings.tool_policy_profile) == "interactive", (
        "an inbox turn ran the saved profile over --tool-profile")


def test_an_inbox_turn_under_the_flag_REFUSES_a_destructive_call(tmp_path: Path) -> None:
    """The card's arm: interactive flag over a convo saved autonomous, inbox mail
    whose tool call is destructive_irreversible — refused, not executed. The
    refusal is `needs_confirmation` on an UNATTENDED source (`_authorize_action`)."""
    host = _Host("autonomous", "interactive", tmp_path)
    profile = _accept(host, "harness", host.settings.tool_policy_profile)
    decision = tool_policy.evaluate(
        profile, tool_policy.SHELL_POLICY, {"command": "rm -rf build"}, tmp_path,
        tool_name="shell")

    # A workspace deletion: DANGER_TABLE's deletion class and NOT under the T1026
    # deny floor, which refuses every profile and so could not tell them apart.
    assert "harness" in tool_policy.UNATTENDED_SOURCES
    assert decision.needs_confirmation, f"{profile} let an unattended rm -rf through: {decision}"


def test_CONTROL_the_same_call_under_autonomous_is_not_stopped(tmp_path: Path) -> None:
    """Without this the arm above passes for a policy that refuses everything."""
    decision = tool_policy.evaluate(
        "autonomous", tool_policy.SHELL_POLICY, {"command": "rm -rf build"}, tmp_path,
        tool_name="shell")
    assert decision.allowed, decision


def test_inbox_mail_NARROWS_and_never_widens(tmp_path: Path) -> None:
    host = _Host("interactive", None, tmp_path)
    assert _accept(host, "harness", "autonomous") == "interactive"
    assert _accept(host, "child-result", "autonomous") == "interactive"
    assert _accept(host, "harness", "strict") == "strict", "a narrower request is honoured"


def test_a_cron_fire_is_AUTONOMOUS_without_a_flag_and_capped_by_one(tmp_path: Path) -> None:
    """T085 (Ryan): a schedule runs autonomous. The flag is a ceiling on every
    source, so a seat launched --tool-profile interactive does not escalate."""
    # T1049: autonomous exists only in Ryan's own instance (owner-marked, not spawned).
    own = _Host("interactive", None, tmp_path)
    own._spawned_seat, own._owner_seat = False, True
    assert _accept(own, "scheduled", "autonomous") == "autonomous"
    assert _accept(_Host("autonomous", "interactive", tmp_path), "scheduled", "autonomous") == "interactive"


def test_a_goal_continuation_narrows_to_the_seat(tmp_path: Path) -> None:
    host = _Host("autonomous", "interactive", tmp_path)
    assert _accept(host, "queued", "autonomous", goal_continuation=True) == "interactive"


def test_a_DELIBERATE_choice_retires_the_flag(tmp_path: Path, monkeypatch) -> None:
    """shift+tab / the wire is a choice made after launch; it supersedes the flag
    the way /model supersedes --model (retire_cli_model)."""
    from litetui import settings_runtime

    monkeypatch.setattr(settings_runtime, "persist_or_raise", lambda app, s: None)
    host = _Host("autonomous", "interactive", tmp_path)
    host.convo_dir = tmp_path
    host._refresh_ctx_label = lambda: None
    host._system = lambda text: None
    app_mod.LiteTUI.set_tool_profile(host, "strict", announce=False)

    assert host._cli_tool_profile is None
    assert _accept(host, "typed", host.chosen_tool_profile) == "strict"


@pytest.mark.asyncio
async def test_a_STEERED_cron_item_is_hooked_under_the_flag_not_autonomous(tmp_path, monkeypatch):
    """Dijkstra R1: codex_steering.HostSteering.admit dispatched prompt_before
    with the RAW producer profile, so a cron item steered into a Codex turn on an
    --tool-profile interactive seat authorized its hook under AUTONOMOUS."""
    from litetui.codex_steering import HostSteering

    host = _Host("autonomous", "interactive", tmp_path)
    host.hook_config, host._stop_requested = object(), False
    seen = []

    async def dispatch(app, event, data, *, profile=None, **_kw):
        seen.append(profile)
        return SimpleNamespace(allowed=True, reason="")

    async def drain(_app):
        pass

    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    monkeypatch.setattr(hook_host, "drain_lifecycle", drain)
    steering = HostSteering(host, None, "thread", "turn", {}, lambda: None)
    await steering.admit({"content": "nightly", "source": "scheduled", "tool_profile": "autonomous"})

    assert seen == ["interactive"], seen


def test_the_two_turns_that_skip_accept_prompt_use_the_resolver() -> None:
    """goal_loop and agent_parent_wake call _stream directly; a producer that
    writes `_active_tool_profile` itself is the divergence this card removes."""
    root = Path(app_mod.__file__).parent
    for name in ("goal_loop.py", "agent_parent_wake.py"):
        src = (root / name).read_text(encoding="utf-8")
        assert "seat_authority" in src, f"{name} stamps its own authority"


# ── 2. the born file records the identity the seat actually launched with ───


class _BornHost:
    from litetui.app import LiteTUI as _Real

    _adopt_convo_settings = _Real._adopt_convo_settings
    _adopt_convo_backend = _Real._adopt_convo_backend
    chosen_tool_profile = _Real.chosen_tool_profile
    model_id = _Real.model_id
    thinking_level = _Real.thinking_level
    backend = _Real.backend
    _remember_for_this_convo = _Real._remember_for_this_convo
    remember_load_settings = _Real.remember_load_settings

    def __init__(self, settings, convo_dir, backend_name):
        self.settings = settings
        self.convo_dir = convo_dir
        self.seat = SimpleNamespace(name="AcceptT1031", agent_id="7f84892f", tier="worker")
        self.available_models: list[str] = []
        self.said: list[str] = []
        self._convo_settings = None
        self._model_id = ""
        self._thinking_level = None
        self._backend = SimpleNamespace(name=backend_name)
        self._active_tool_profile = settings.tool_policy_profile
        self._invocation_saved_values: dict = {}
        self._launch_overrides: dict = {}
        self._cli_initial_backend = None
        self._cli_initial_model = None
        self._cli_thinking_level = None
        self._cli_tool_profile = None

    def _system(self, text):
        self.said.append(text)

    def _resume_cli_convo(self):
        return True


def _launched_like_AcceptT1031(tmp_path: Path, flag_profile: str, saved_profile: str) -> _BornHost:
    """The measured seat: global codex / gpt-6-sol, launched
    `--backend claude --model claude-opus-5-5 --thinking-level high --tool-profile X`.
    `capture_invocation` has already put the flag backend into settings and kept
    the disk value in `_invocation_saved_values` (app.py:1532)."""
    s = st.Settings(backend="claude", default_model="gpt-6-sol", thinking_level="medium",
                    tool_policy_profile=saved_profile)
    h = _BornHost(s, tmp_path, "claude")
    h._invocation_saved_values = {"backend": "codex"}
    h._cli_initial_backend = "claude"
    h._cli_initial_model = "claude-opus-5-5"
    h._model_id = "claude-opus-5-5"
    h._cli_thinking_level = "high"
    h._thinking_level = "high"
    h._cli_tool_profile = flag_profile
    return h


def test_a_flag_launched_convo_is_BORN_with_the_flags_identity(tmp_path: Path) -> None:
    """Measured: .convos/ec62c953 recorded codex / gpt-6-sol for a seat running
    claude-opus-5-5 — so a RESUME of it would have run Codex."""
    h = _launched_like_AcceptT1031(tmp_path, "interactive", "autonomous")
    h._adopt_convo_settings(born=True)
    on_disk = json.loads(cs_mod.path_for(tmp_path).read_text(encoding="utf-8"))

    assert (on_disk["backend"], on_disk["model"], on_disk["thinking_level"]) == (
        "claude", "claude-opus-5-5", "high")
    ex = on_disk["execution"]
    assert (ex["backend"], ex["default_model"], ex["thinking_level"]) == (
        "claude", "claude-opus-5-5", "high")


def test_a_STRICTER_flag_profile_is_born_into_the_file(tmp_path: Path) -> None:
    """Sentinel 0118549d: a restriction is not a grant, so it may outlive the
    invocation."""
    h = _launched_like_AcceptT1031(tmp_path, "interactive", "autonomous")
    h._adopt_convo_settings(born=True)
    on_disk = json.loads(cs_mod.path_for(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["tool_policy_profile"] == "interactive"
    assert on_disk["execution"]["tool_policy_profile"] == "interactive"


def test_a_LOOSER_flag_profile_is_never_born_into_the_file(tmp_path: Path) -> None:
    """T695 stands for grants: an autonomous flag must not outlive its run."""
    h = _launched_like_AcceptT1031(tmp_path, "autonomous", "interactive")
    h._adopt_convo_settings(born=True)
    on_disk = json.loads(cs_mod.path_for(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["tool_policy_profile"] == "interactive"
    assert on_disk["execution"]["tool_policy_profile"] == "interactive"


def test_CONTROL_an_unflagged_birth_still_copies_the_globals(tmp_path: Path) -> None:
    s = st.Settings(backend="codex", default_model="gpt-6-sol", thinking_level="medium",
                    tool_policy_profile="interactive")
    h = _BornHost(s, tmp_path, "codex")
    h._model_id = "gpt-6-sol"
    h._adopt_convo_settings(born=True)
    on_disk = json.loads(cs_mod.path_for(tmp_path).read_text(encoding="utf-8"))
    assert (on_disk["backend"], on_disk["model"], on_disk["thinking_level"],
            on_disk["tool_policy_profile"]) == ("codex", "gpt-6-sol", "medium", "interactive")


def test_a_resume_that_SWITCHES_backend_says_so_out_loud(tmp_path: Path, monkeypatch) -> None:
    cs_mod.save(tmp_path, cs_mod.ConvoSettings(backend="codex", model="gpt-6-sol"))
    monkeypatch.setattr(app_mod.llm_backend, "make_backend", lambda s: SimpleNamespace(name=s.backend))
    h = _BornHost(st.Settings(backend="claude"), tmp_path, "claude")
    h._cli_initial_backend = "claude"
    h._invocation_saved_values = {"backend": "codex"}
    h._adopt_convo_settings(born=False)

    assert h.backend.name == "claude", "the explicit flag still wins"
    assert any("codex" in t and "claude" in t for t in h.said), (
        f"a resume switched codex -> claude silently: {h.said}")


def test_CONTROL_a_resume_on_its_born_backend_is_quiet(tmp_path: Path, monkeypatch) -> None:
    cs_mod.save(tmp_path, cs_mod.ConvoSettings(backend="claude", model="claude-opus-5-5"))
    monkeypatch.setattr(app_mod.llm_backend, "make_backend", lambda s: SimpleNamespace(name=s.backend))
    h = _BornHost(st.Settings(backend="codex"), tmp_path, "codex")
    h._adopt_convo_settings(born=False)

    assert h.backend.name == "claude", "the convo's own backend, not the global"
    assert not any("switch" in t.lower() for t in h.said), h.said


# ── 3. a taken name auto-changes; the real registry, in a throwaway HOME ─────


def _registry_python() -> str | None:
    """The interpreter the hooks run liteharness with: the venv's base python."""
    for name in ("python.exe", "python3", "python"):
        p = Path(sys.base_prefix) / name
        if p.exists():
            probe = subprocess.run([str(p), "-c", "import liteharness"], capture_output=True)
            return str(p) if probe.returncode == 0 else None
    return None


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """Route harness._cli to the REAL liteharness with HOME at tmp_path.

    Each call asserts, inside the child and before liteharness is imported,
    that Path.home() IS the temp dir — a HOME override that did not take must
    fail the child, never write the live ~/.liteharness."""
    py = _registry_python()
    if py is None:
        pytest.skip("no interpreter with liteharness installed")
    home = tmp_path / "home"
    root = home / ".liteharness"
    (root / "agents").mkdir(parents=True)
    (root / "names").mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "LITEHARNESS_NO_ANNOUNCE": "1", "LITEHARNESS_HOME": str(root)}
    env.pop(harness_mod.NO_HARNESS_ENV, None)

    def _cli(args, *, timeout):
        boot = (
            "import sys, pathlib\n"
            f"assert str(pathlib.Path.home()) == {str(home)!r}, pathlib.Path.home()\n"
            "from liteharness import cli, config\n"
            f"assert str(config.get_root()) == {str(root)!r}, config.get_root()\n"
            "sys.argv = ['liteharness', *sys.argv[1:]]\n"
            "cli.main()\n"
        )
        return subprocess.run([py, "-c", boot, *args], env=env, capture_output=True,
                              text=True, timeout=timeout)

    monkeypatch.setattr(harness_mod, "_cli", _cli)
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    return root


def _holder(root: Path, name: str, pid: int, quiet_s: int) -> str:
    """Registered NOW, quiet for `quiet_s`: a record registered before its pid's
    process started reads as a REUSED pid (liteharness T1027 L1), and the
    sleeper here was started moments ago."""
    agent = str(uuid.uuid4())
    seen = (datetime.now(timezone.utc) - timedelta(seconds=quiet_s)).isoformat()
    (root / "agents" / f"{agent}.json").write_text(json.dumps({
        "agent_id": agent, "name": name, "cli": "litetui", "session_pid": pid,
        "last_seen": seen, "registered_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8")
    (root / "names" / agent).write_text(name, encoding="utf-8")
    return agent


def test_a_second_OpenBolt_becomes_OpenBolt_2_and_the_first_keeps_its_name(registry) -> None:
    """Ryan (liteask a-1db0f560): "the agents name is supposed to auto change if
    the name is taken ... openbolt is the user choosen default name". The holder
    is Ryan's own seat: pid alive, quiet for 700 s — past --takeover's 600 s bar,
    well inside the 43200 s bar a plain register uses."""
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        ryan = _holder(registry, "OpenBolt", sleeper.pid, 700)
        seat = harness_mod.Seat(agent_id=str(uuid.uuid4()), name="OpenBolt", model="m")
        assert seat.register(), seat.error

        assert seat.name == "OpenBolt-2"
        assert (registry / "agents" / f"{ryan}.json").exists(), "Ryan's row was evicted"
        assert (registry / "names" / ryan).read_text(encoding="utf-8") == "OpenBolt"
    finally:
        sleeper.kill()


def test_a_relaunched_seat_reclaims_its_name_from_its_own_DEAD_corpse(registry) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _holder(registry, "OpenBolt", dead.pid, 30)
    seat = harness_mod.Seat(agent_id=str(uuid.uuid4()), name="OpenBolt", model="m")
    assert seat.register(), seat.error
    assert seat.name == "OpenBolt"


# ── 4. presence reports what the seat resolved ───────────────────────────────


def test_presence_carries_the_resolved_backend_and_thinking() -> None:
    seat = harness_mod.Seat(agent_id="i", name="n", model="gpt-6-sol")
    seat.backend, seat.thinking_level = "codex", "high"
    argv = seat._presence_argv()
    assert argv[argv.index("--backend") + 1] == "codex"
    assert argv[argv.index("--thinking-level") + 1] == "high"


def test_CONTROL_an_unknown_backend_sends_no_flag() -> None:
    argv = harness_mod.Seat(agent_id="i", name="n", model="m")._presence_argv()
    assert "--backend" not in argv


def test_every_beat_syncs_the_backend_the_seat_is_on() -> None:
    """T1025's _sync_seat_resolution runs before register and every heartbeat;
    the backend rides it, so a /backend switch reaches presence within a beat."""
    seat = harness_mod.Seat(agent_id="i", name="n", model="")
    app = SimpleNamespace(seat=seat, model_id="o4-mini", backend=SimpleNamespace(name="codex"),
                          _thinking_level=None)
    app_mod._sync_seat_resolution(app)
    assert (seat.backend, seat.model) == ("codex", "o4-mini")
