"""T691 — each conversation carries its own model configuration.

Ryan (a-62edbbe0): *"per convo settings files json that save backend model
liteharness-info think level ... and everything llama and lmstudio support"*.

⚠️ EVERY ARM USES A TEMP DIRECTORY. `.convos/` is live on this box with hundreds
of real conversations in it.
"""

from __future__ import annotations

import json
from pathlib import Path

from litetui import app as app_mod
from litetui import convo_settings as cs_mod
from litetui import settings as st


def _dir(tmp_path: Path, name: str = "convo-a") -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def test_a_new_conversation_is_born_from_the_global_defaults(tmp_path: Path) -> None:
    s = st.Settings()
    s.backend = "llamacpp"
    s.default_model = "qwen/qwen3-8b"
    s.thinking_level = "xhigh"

    cs = cs_mod.born_from(s)

    assert cs.backend == "llamacpp"
    assert cs.model == "qwen/qwen3-8b"
    assert cs.thinking_level == "xhigh"


def test_a_conversation_with_NO_file_falls_through_to_the_globals(tmp_path: Path) -> None:
    """Every conversation that existed before this card is in this state, and
    must behave exactly as it did — absent is not empty."""
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    cs = cs_mod.load(_dir(tmp_path))

    assert cs.model is None
    assert cs_mod.resolved(cs, s, "model") == "gemma-3-4b-it.Q4_K_M"


def test_a_choice_made_IN_a_conversation_beats_the_global_default(tmp_path: Path) -> None:
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/qwen3-8b"))

    assert cs_mod.resolved(cs_mod.load(d), s, "model") == "qwen/qwen3-8b"


def test_OFF_is_a_choice_and_not_an_absence(tmp_path: Path) -> None:
    """🔴 THE FALSY TRAP. `thinking_level` can legitimately be "off". Treating a
    falsy value as "not chosen" would make "off" unstorable — the conversation
    would silently revert to the global level every time it was opened, and the
    user would see their explicit choice undone with nothing said."""
    s = st.Settings()
    s.thinking_level = "xhigh"
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(thinking_level="off"))

    assert cs_mod.resolved(cs_mod.load(d), s, "thinking_level") == "off"


def test_two_conversations_do_not_see_each_other(tmp_path: Path) -> None:
    """The whole point of the card: a model switch in A must not move B."""
    a, b = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    cs_mod.save(a, cs_mod.ConvoSettings(model="qwen/qwen3-8b", thinking_level="xhigh"))
    cs_mod.save(b, cs_mod.ConvoSettings(model="gemma-3-4b-it.Q4_K_M", thinking_level="off"))

    assert cs_mod.load(a).model == "qwen/qwen3-8b"
    assert cs_mod.load(b).model == "gemma-3-4b-it.Q4_K_M"
    assert cs_mod.load(a).thinking_level == "xhigh"
    assert cs_mod.load(b).thinking_level == "off"


def test_writing_a_conversation_never_touches_the_GLOBAL_settings(tmp_path: Path) -> None:
    """Stated as its own arm because it is the rule most easily broken by a
    convenience: `settings.json` keeps being the DEFAULTS plus the app-wide
    knobs, and a per-conversation choice must not leak into it."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    st.save(st.Settings(), global_root)
    before = st.settings_path(global_root).read_bytes()

    cs_mod.save(_dir(tmp_path), cs_mod.ConvoSettings(model="qwen/qwen3-8b"))

    assert st.settings_path(global_root).read_bytes() == before


def test_the_file_carries_the_backend_specific_load_settings(tmp_path: Path) -> None:
    """Ryan named these: *"everything llama and lmstudio support / need for
    their specifics"*. They are separate fields because the two runtimes take
    different keys, and one merged dict would make a llama ctx look like an
    LM Studio one."""
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(
        llama_load={"ctx": 8192, "gpu_layers": 99},
        lmstudio_load={"ctx": 4096, "ttl": 3600},
    ))
    got = cs_mod.load(d)

    assert got.llama_load == {"ctx": 8192, "gpu_layers": 99}
    assert got.lmstudio_load == {"ctx": 4096, "ttl": 3600}


def test_the_file_carries_the_liteharness_identity(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(
        seat_name="OpenBolt", seat_id="2578f274", seat_tier="worker"))
    got = cs_mod.load(d)

    assert (got.seat_name, got.seat_id, got.seat_tier) == ("OpenBolt", "2578f274", "worker")


def test_an_unreadable_file_opens_the_conversation_anyway(tmp_path: Path) -> None:
    """🔴 THE TRANSCRIPT IS THE VALUABLE THING. A hand-edited or truncated
    config must never be the reason a conversation cannot be opened — it falls
    through to the globals, exactly like an absent one."""
    d = _dir(tmp_path)
    cs_mod.path_for(d).write_text("{not json", encoding="utf-8")

    cs = cs_mod.load(d)
    assert cs.model is None
    assert cs_mod.resolved(cs, st.Settings(), "model") == st.Settings().default_model


def test_a_save_is_atomic_and_leaves_no_litter(tmp_path: Path) -> None:
    """Same rule as the global file (T688): `load` answers an unparseable file
    with SILENT defaults, so a reader landing mid-write would see the
    conversation quietly revert rather than fail in a way anyone could act on.
    """
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(model="a"))
    cs_mod.save(d, cs_mod.ConvoSettings(model="b"))

    strays = [p.name for p in d.iterdir() if p.name.startswith(".settings-")]
    assert strays == [], f"a temp file survived the save: {strays}"
    assert json.loads(cs_mod.path_for(d).read_text(encoding="utf-8"))["model"] == "b"


# ── the App writes through, and reads back ───────────────────────────────────


class _FakeBackend:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeSeat:
    name = "OpenBolt"
    agent_id = "2578f274"
    tier = "worker"


class _App:
    """The smallest thing that exercises the App's write-through.

    ⬜ NOT a Textual pilot: the property setters and `_adopt_convo_settings` are
    plain Python, and driving a whole terminal to assert a JSON file would make
    a slow test of a fast fact. The pilot arms that DO run the app already cover
    mounting; what is new here is the persistence, and it has no widgets in it.
    """

    from litetui.app import LiteTUI as _Real

    model_id = _Real.model_id
    thinking_level = _Real.thinking_level
    backend = _Real.backend
    chosen_tool_profile = _Real.chosen_tool_profile
    set_tool_profile = _Real.set_tool_profile
    _remember_for_this_convo = _Real._remember_for_this_convo
    _adopt_convo_settings = _Real._adopt_convo_settings
    _adopt_convo_backend = _Real._adopt_convo_backend
    remember_load_settings = _Real.remember_load_settings

    def __init__(self, settings, convo_dir, backend_name="llamacpp"):
        self.settings = settings
        self.convo_dir = convo_dir
        self.seat = _FakeSeat()
        self.available_models: list[str] = []
        self.said: list[str] = []
        self._convo_settings = None
        self._model_id = ""
        self._thinking_level = None
        self._backend = _FakeBackend(backend_name)
        self._active_tool_profile = settings.tool_policy_profile
        self._cli_tool_profile = None

    def _system(self, text):
        self.said.append(text)

    def _refresh_ctx_label(self):
        # `set_tool_profile` repaints the footer chip. Stubbed rather than
        # avoided: driving the REAL method is the whole point — T691 shipped a
        # field that round-tripped through the file and was written by nothing,
        # because its arm exercised the dataclass instead of the app.
        pass


def test_a_NEW_conversation_writes_its_file_from_the_globals(tmp_path: Path) -> None:
    s = st.Settings()
    s.default_model = "qwen/qwen3-8b"
    s.thinking_level = "xhigh"
    d = _dir(tmp_path)

    a = _App(s, d)
    a._adopt_convo_settings(born=True)

    on_disk = json.loads(cs_mod.path_for(d).read_text(encoding="utf-8"))
    assert on_disk["model"] == "qwen/qwen3-8b"
    assert on_disk["thinking_level"] == "xhigh"
    assert on_disk["seat_name"] == "OpenBolt", "the owning seat travels with it"


def test_a_model_switch_writes_THIS_conversation_and_not_the_other(tmp_path: Path) -> None:
    """🔴 THE CARD IN ONE ARM. Two conversations, a switch in one."""
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    a_dir, b_dir = _dir(tmp_path, "a"), _dir(tmp_path, "b")

    a, b = _App(s, a_dir), _App(s, b_dir)
    a._adopt_convo_settings(born=True)
    b._adopt_convo_settings(born=True)

    a.model_id = "qwen/qwen3-8b"        # the assignment a plugin makes

    assert cs_mod.load(a_dir).model == "qwen/qwen3-8b"
    assert cs_mod.load(b_dir).model == "gemma-3-4b-it.Q4_K_M", "B moved"
    assert s.default_model == "gemma-3-4b-it.Q4_K_M", "the GLOBAL default moved"


def test_a_startup_assignment_writes_nothing(tmp_path: Path) -> None:
    """`__init__` sets both fields before any conversation exists. A setter that
    wrote unconditionally would create a settings file for a conversation that
    has not been started — and, worse, in whatever directory happened to be
    current."""
    a = _App(st.Settings(), _dir(tmp_path))
    a.model_id = "qwen/qwen3-8b"
    a.thinking_level = "xhigh"

    assert not cs_mod.path_for(a.convo_dir).exists()


def test_re_assigning_the_SAME_value_does_not_rewrite_the_file(tmp_path: Path) -> None:
    """These setters fire on every assignment, including a `/model` to the model
    already selected and a reconnect re-applying the current level. Writing per
    assignment would rewrite the file several times a turn for no change."""
    d = _dir(tmp_path)
    a = _App(st.Settings(), d)
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"
    stamp = cs_mod.path_for(d).stat().st_mtime_ns

    a.model_id = "qwen/qwen3-8b"

    assert cs_mod.path_for(d).stat().st_mtime_ns == stamp


def test_resume_applies_the_conversation_file(tmp_path: Path) -> None:
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/qwen3-8b", thinking_level="high"))

    a = _App(s, d)
    a._adopt_convo_settings(born=False)

    assert a.model_id == "qwen/qwen3-8b"
    assert a.thinking_level == "high"


def test_applying_a_stored_value_does_not_write_it_back(tmp_path: Path) -> None:
    """⚠️ THE LOOP THIS AVOIDS. `_adopt_convo_settings` assigns through the
    BACKING fields, not the properties: restoring a stored choice is not a new
    choice, and routing it through the setter would rewrite the file on every
    open — and on a read-only copy, fail on every open."""
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/qwen3-8b"))
    stamp = cs_mod.path_for(d).stat().st_mtime_ns

    a = _App(st.Settings(), d)
    a._adopt_convo_settings(born=False)

    assert cs_mod.path_for(d).stat().st_mtime_ns == stamp


def test_a_model_the_server_no_longer_has_falls_back_AND_SAYS_SO(tmp_path: Path) -> None:
    """Silently answering in another model's voice while the header still shows
    the old name is worse than any error."""
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/gone-7b"))

    a = _App(s, d)
    a.available_models = ["gemma-3-4b-it.Q4_K_M", "qwen/qwen3-8b"]
    a._adopt_convo_settings(born=False)

    assert a.model_id == "gemma-3-4b-it.Q4_K_M"
    assert any("qwen/gone-7b" in m for m in a.said), a.said


def test_an_EMPTY_model_list_is_unknown_and_never_triggers_the_fallback(tmp_path: Path) -> None:
    """`available_models` is empty until `connect()` has listed. Reading that as
    "the server has nothing" would rewrite every conversation to the default on
    every start, before the app had even asked what is installed."""
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/qwen3-8b"))

    a = _App(s, d)
    a.available_models = []
    a._adopt_convo_settings(born=False)

    assert a.model_id == "qwen/qwen3-8b"
    assert a.said == []


# ── the four fields the first cut declared and never wired ───────────────────
#
# 🔴 A DECLARED FIELD READS AS A CARRIED FIELD. The first cut of this card put
# `backend`, `reasoning_effort`, `llama_load` and `lmstudio_load` in the
# dataclass and wired none of them — and the arms above PASSED, because they
# asserted the FILE round-trips those keys. It does. What no arm asked was
# whether anything in the app ever writes or reads them, and a dataclass will
# happily round-trip a field the product never touches.
#     A SCHEMA IS A PROMISE ABOUT SHAPE, NOT ABOUT BEHAVIOUR.
# Each arm below drives the APP, not the file.


def test_a_backend_switch_is_remembered_by_the_conversation(tmp_path: Path) -> None:
    """Ryan named backend FIRST, and the first cut recorded it nowhere."""
    d = _dir(tmp_path)
    a = _App(st.Settings(), d)
    a._adopt_convo_settings(born=True)

    a.backend = _FakeBackend("codex")      # what /backend assigns

    assert cs_mod.load(d).backend == "codex"


def test_a_backend_switch_in_A_does_not_move_B(tmp_path: Path) -> None:
    s = st.Settings()
    a_dir, b_dir = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    a, b = _App(s, a_dir), _App(s, b_dir)
    a._adopt_convo_settings(born=True)
    b._adopt_convo_settings(born=True)

    a.backend = _FakeBackend("codex")

    assert cs_mod.load(a_dir).backend == "codex"
    assert cs_mod.load(b_dir).backend == s.backend


def test_opening_a_conversation_puts_it_back_on_its_own_engine(tmp_path: Path, monkeypatch) -> None:
    """And through the FACTORY, so T690's VRAM gate is stamped on the rebuilt
    backend — assigning a hand-made one would hand the app an ungated engine and
    the modal would stop appearing for exactly the people who switch engines."""
    from litetui import llm_backend

    made: list[str] = []

    def _fake_factory(settings):
        made.append(settings.backend)
        return _FakeBackend(settings.backend)

    monkeypatch.setattr(llm_backend, "make_backend", _fake_factory)

    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(backend="codex"))
    a = _App(st.Settings(), d, backend_name="llamacpp")
    a._adopt_convo_settings(born=False)

    assert made == ["codex"], "the factory was not asked for the convo's engine"
    assert a.backend.name == "codex"


def test_an_engine_the_box_no_longer_has_falls_back_AND_SAYS_SO(tmp_path: Path, monkeypatch) -> None:
    from litetui import llm_backend

    def _boom(settings):
        raise llm_backend.BackendError("that backend is unavailable")

    monkeypatch.setattr(llm_backend, "make_backend", _boom)

    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(backend="codex"))
    a = _App(st.Settings(), d, backend_name="llamacpp")
    a._adopt_convo_settings(born=False)

    assert a.backend.name == "llamacpp", "it switched to an engine that cannot be built"
    assert any("codex" in m for m in a.said), a.said


def test_the_codex_effort_is_kept_in_its_OWN_field(tmp_path: Path) -> None:
    """Ryan: *"think level when on codex"*. It is a different vocabulary from
    LM Studio's thinking levels, so a conversation carried between engines must
    not hand a codex effort to a llama.cpp level."""
    d = _dir(tmp_path)
    a = _App(st.Settings(), d, backend_name="codex")
    a._adopt_convo_settings(born=True)

    a.thinking_level = "high"

    got = cs_mod.load(d)
    assert got.reasoning_effort == "high"
    assert got.thinking_level == "high"


def test_a_NON_codex_level_does_not_write_the_codex_field(tmp_path: Path) -> None:
    """The discriminator. Without it the arm above is satisfied by a setter that
    writes both fields unconditionally, which is exactly the conflation the
    separate field exists to prevent."""
    d = _dir(tmp_path)
    a = _App(st.Settings(), d, backend_name="llamacpp")
    a._adopt_convo_settings(born=True)

    a.thinking_level = "xhigh"

    got = cs_mod.load(d)
    assert got.thinking_level == "xhigh"
    assert got.reasoning_effort is None


def test_llama_load_settings_are_remembered_for_THIS_model(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    a = _App(st.Settings(), d, backend_name="llamacpp")
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"

    a.remember_load_settings("qwen/qwen3-8b", {"ctx": 8192, "gpu_layers": 99})

    assert cs_mod.load(d).llama_load == {"ctx": 8192, "gpu_layers": 99}


def test_load_settings_for_ANOTHER_model_are_not_claimed(tmp_path: Path) -> None:
    """`/modelcfg` can edit a model the conversation is not using. Recording
    that here would make the conversation claim a configuration it never ran."""
    d = _dir(tmp_path)
    a = _App(st.Settings(), d, backend_name="llamacpp")
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"

    a.remember_load_settings("some/other-model", {"ctx": 512})

    assert cs_mod.load(d).llama_load == {}


def test_lmstudio_load_settings_land_in_the_LMSTUDIO_field(tmp_path: Path) -> None:
    """Two runtimes, two key vocabularies. One merged dict would make a llama
    ctx indistinguishable from an LM Studio one when the conversation moves."""
    d = _dir(tmp_path)
    a = _App(st.Settings(), d, backend_name="lmstudio")
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"

    a.remember_load_settings("qwen/qwen3-8b", {"ctx": 4096})

    got = cs_mod.load(d)
    assert got.lmstudio_load == {"ctx": 4096}
    assert got.llama_load == {}


def test_opening_a_conversation_restores_its_load_settings_and_effort(tmp_path: Path) -> None:
    """The READ half. The first cut had writers for nothing and readers for two
    fields; these are the ones that had neither."""
    s = st.Settings()
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(
        model="qwen/qwen3-8b",
        llama_load={"ctx": 8192},
        reasoning_effort="high",
    ))

    a = _App(s, d)
    a._adopt_convo_settings(born=False)

    assert a.settings.llama_load_settings["qwen/qwen3-8b"] == {"ctx": 8192}
    assert (a.settings.model_infer_overrides["qwen/qwen3-8b"]["reasoning_effort"]) == "high"


def test_EVERY_declared_field_has_a_writer_or_is_named_as_carried_only() -> None:
    """🔴 THE ARM THAT WOULD HAVE CAUGHT THE FIRST CUT, derived from the
    dataclass rather than from a list.

    A field in `ConvoSettings` that nothing ever writes is a promise the file
    makes and the app does not keep. `tool_policy_profile` is the one deliberate
    exception — it is CARRIED so the shape is stable, and its write-through is
    its own card, because several of its eight assignment sites are transient
    (a goal-loop override, two restore paths) and persisting those would record
    a temporary elevation as the conversation's standing choice.
    """
    from dataclasses import fields as fields_of
    from pathlib import Path as _P

    app_src = _P(app_mod.__file__).read_text(encoding="utf-8")
    carried_only = {"seat_name", "seat_id", "seat_tier"}

    missing = []
    for f in fields_of(cs_mod.ConvoSettings):
        if f.name in carried_only:
            continue
        if f'"{f.name}"' not in app_src:
            missing.append(f.name)

    assert missing == [], (
        f"{missing} are declared in ConvoSettings and never named in app.py — "
        f"a field the file round-trips and the app never writes is a promise "
        f"the schema makes and the product does not keep"
    )
    # The validity gate: an all-exempt list would satisfy the loop above.
    assert len(carried_only) < len(fields_of(cs_mod.ConvoSettings))


# ── T695: the CHOSEN profile, separated from every transient one ─────────
#
# 🔴 NINE THINGS WRITE `_active_tool_profile` AND EXACTLY ONE IS A CHOICE.
# Inbox mail degrades through `unattended()` (app.py:1630), a cron or loop fire
# pins AUTONOMOUS (app.py:1699), a goal loop restores its own
# (goal_loop.py:326), the flush re-stamps whatever a queued item carried
# (app.py:5541/5569), and `--tool-profile` overrides for the process
# (app.py:1226). Persisting any of those would record a temporary elevation as
# the conversation's standing choice — one scheduled job and the chat is
# autonomous for good.
#     `set_tool_profile` (shift+tab and the wire) is the only CHOICE, so it is
# the only writer, and `chosen_tool_profile` is the source the two
# non-transient reads consult.


def test_an_explicit_authority_change_is_remembered_by_the_conversation(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    a = _App(st.Settings(), d)
    a._adopt_convo_settings(born=True)

    a.set_tool_profile("interactive", announce=False)

    assert cs_mod.load(d).tool_policy_profile == "interactive"
    assert a._active_tool_profile == "interactive"


def test_an_authority_change_in_A_does_not_move_B(tmp_path: Path) -> None:
    s = st.Settings()
    a_dir, b_dir = _dir(tmp_path, "a"), _dir(tmp_path, "b")
    a, b = _App(s, a_dir), _App(s, b_dir)
    a._adopt_convo_settings(born=True)
    b._adopt_convo_settings(born=True)

    before = cs_mod.load(b_dir).tool_policy_profile

    a.set_tool_profile("interactive", announce=False)

    assert cs_mod.load(a_dir).tool_policy_profile == "interactive"
    # ⚠️ AGAINST THE SNAPSHOT, NOT AGAINST `s.tool_policy_profile`. An explicit
    # choice also moves the GLOBAL default (existing behaviour, unchanged by
    # this card), so comparing B against the live settings object compares it
    # with something the treatment just edited — and the arm would fail while
    # B was in fact untouched.
    assert cs_mod.load(b_dir).tool_policy_profile == before


def test_opening_a_conversation_puts_it_back_on_its_own_authority(tmp_path: Path) -> None:
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"))
    s = st.Settings()
    s.tool_policy_profile = "autonomous"

    a = _App(s, d)
    a._adopt_convo_settings(born=False)

    assert a._active_tool_profile == "interactive"
    assert a.chosen_tool_profile == "interactive"


def test_a_TRANSIENT_elevation_is_not_recorded_as_the_choice(tmp_path: Path) -> None:
    """🔴 THE DISCRIMINATING ARM, and the reason this was not done with T691.

    Every other field could be wired by writing through its setter; this one
    cannot, because most of what assigns it is a TURN-SCOPED override. A cron
    fire assigning AUTONOMOUS directly — exactly what `app.py:1699` does — must
    leave the conversation remembered authority alone.

    Without this, a wiring that simply made `_active_tool_profile` a
    write-through property would pass every other arm in this file and quietly
    make one scheduled job the conversation standing authority.
    """
    d = _dir(tmp_path)
    s = st.Settings()
    s.tool_policy_profile = "interactive"
    a = _App(s, d)
    a._adopt_convo_settings(born=True)
    a.set_tool_profile("interactive", announce=False)

    a._active_tool_profile = "autonomous"      # a cron fire, verbatim

    assert cs_mod.load(d).tool_policy_profile == "interactive"
    assert a.chosen_tool_profile == "interactive"


def test_the_chosen_profile_falls_through_when_the_conversation_never_chose(tmp_path: Path) -> None:
    """A conversation that predates this card has no file and must behave
    exactly as it did — absent is not empty."""
    s = st.Settings()
    s.tool_policy_profile = "autonomous"
    a = _App(s, _dir(tmp_path))
    a._adopt_convo_settings(born=False)

    assert a.chosen_tool_profile == "autonomous"


# ── the two rulings, each with an arm ────────────────────────────────────


def test_a_LOOP_is_stamped_from_the_GLOBAL_setting_not_the_conversation(tmp_path: Path) -> None:
    """🔴 RYAN RULING, ALREADY IN THE CODE AT app.py:1672, AND BINDING:
    changing how autonomous the CHAT is must not silently change what every
    saved automation may do. So `goal_loop.py:372` reads
    `settings.tool_policy_profile` and deliberately NOT this conversation.

    Stated as an arm rather than left to the comment, because a per-convo card
    is exactly when someone "finishes the job" by pointing this at the
    conversation too.
    """
    from litetui import goal_loop

    s = st.Settings()
    s.tool_policy_profile = "autonomous"
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"))

    a = _App(s, d)
    a._adopt_convo_settings(born=False)
    assert a.chosen_tool_profile == "interactive", "the arm is not testing what it claims"

    state = goal_loop.GoalState(
        objective="ship it",
        tool_profile=getattr(a.settings, "tool_policy_profile", goal_loop.INTERACTIVE),
    )

    assert state.tool_profile == "autonomous", (
        "a saved automation inherited the conversation authority — app.py:1672"
    )


def test_the_CLI_FLAG_wins_on_resume_and_never_writes_the_file(tmp_path: Path) -> None:
    """🔴 SENTINEL RULING: an explicit invocation outranks a stored default,
    and a flag that became sticky would be the opposite of explicit — it would
    outlive the invocation that asked for it and apply to runs that did not."""
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"))
    s = st.Settings()

    a = _App(s, d)
    a._cli_tool_profile = "autonomous"
    a._active_tool_profile = "autonomous"      # what app.py:1226 already did
    a._adopt_convo_settings(born=False)

    assert a._active_tool_profile == "autonomous", "the flag lost to the stored choice"
    assert cs_mod.load(d).tool_policy_profile == "interactive", (
        "the flag wrote itself into the conversation and became sticky"
    )


def test_CONTROL_without_the_flag_the_conversation_wins(tmp_path: Path) -> None:
    """The other half. Without it the arm above passes for a resume that never
    adopts anything at all."""
    d = _dir(tmp_path)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"))
    s = st.Settings()
    s.tool_policy_profile = "autonomous"

    a = _App(s, d)
    a._active_tool_profile = "autonomous"
    a._adopt_convo_settings(born=False)

    assert a._active_tool_profile == "interactive"
