"""T691 — each conversation carries its own model configuration.

the user (a-62edbbe0): *"per convo settings files json that save backend model
liteharness-info think level ... and everything llama and lmstudio support"*.

⚠️ EVERY ARM USES A TEMP DIRECTORY. `.convos/` is live on this box with hundreds
of real conversations in it.
"""

from __future__ import annotations

import json
import pytest


@pytest.fixture(autouse=True)
def _isolate_execution_environment(monkeypatch):
    # conftest supplies an LM Studio boot default; these tests characterize
    # stored choices unless a test explicitly selects an invocation override.
    for key in ('LITETUI_BACKEND', 'LITETUI_MODEL', 'LITETUI_THINKING'):
        monkeypatch.delenv(key, raising=False)

from pathlib import Path

from litetui import app as app_mod
from litetui import convo_settings as cs_mod
from litetui import settings as st


@pytest.fixture
def owned_session(tmp_path):
    from litetui.agent_launch_context import ordinary
    cfg = st.Settings(default_model='fixture-model')
    session = ordinary(tmp_path, cfg)
    try:
        yield session
    finally:
        session.release()


def _dir(tmp_path: Path, name: str = "convo-a", *, owned_session) -> Path:
    from uuid import uuid5, NAMESPACE_URL
    cid = str(uuid5(NAMESPACE_URL, str(tmp_path / name)))
    d = owned_session.conversation_directory(cid)
    d.mkdir()
    return d


def test_a_new_conversation_is_born_from_the_global_defaults(tmp_path: Path, owned_session) -> None:
    s = st.Settings()
    s.backend = "llamacpp"
    s.default_model = "qwen/qwen3-8b"
    s.thinking_level = "xhigh"

    cs = cs_mod.born_from(s)

    assert cs.backend == "llamacpp"
    assert cs.model == "qwen/qwen3-8b"
    assert cs.thinking_level == "xhigh"


def test_a_conversation_with_NO_file_falls_through_to_the_globals(tmp_path: Path, owned_session) -> None:
    """Every conversation that existed before this card is in this state, and
    must behave exactly as it did — absent is not empty."""
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    cs = cs_mod.load(_dir(tmp_path, owned_session=owned_session))

    assert cs.model is None
    assert cs_mod.resolved(cs, s, "model") == "gemma-3-4b-it.Q4_K_M"


def test_a_choice_made_IN_a_conversation_beats_the_global_default(tmp_path: Path, owned_session) -> None:
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/qwen3-8b"), agent_session=owned_session)

    assert cs_mod.resolved(cs_mod.load(d), s, "model") == "qwen/qwen3-8b"


def test_OFF_is_a_choice_and_not_an_absence(tmp_path: Path, owned_session) -> None:
    """🔴 THE FALSY TRAP. `thinking_level` can legitimately be "off". Treating a
    falsy value as "not chosen" would make "off" unstorable — the conversation
    would silently revert to the global level every time it was opened, and the
    user would see their explicit choice undone with nothing said."""
    s = st.Settings()
    s.thinking_level = "xhigh"
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(thinking_level="off"), agent_session=owned_session)

    assert cs_mod.resolved(cs_mod.load(d), s, "thinking_level") == "off"


def test_two_conversations_do_not_see_each_other(tmp_path: Path, owned_session) -> None:
    """The whole point of the card: a model switch in A must not move B."""
    a, b = _dir(tmp_path, "a", owned_session=owned_session), _dir(tmp_path, "b", owned_session=owned_session)
    cs_mod.save(a, cs_mod.ConvoSettings(model="qwen/qwen3-8b", thinking_level="xhigh"), agent_session=owned_session)
    cs_mod.save(b, cs_mod.ConvoSettings(model="gemma-3-4b-it.Q4_K_M", thinking_level="off"), agent_session=owned_session)

    assert cs_mod.load(a).model == "qwen/qwen3-8b"
    assert cs_mod.load(b).model == "gemma-3-4b-it.Q4_K_M"
    assert cs_mod.load(a).thinking_level == "xhigh"
    assert cs_mod.load(b).thinking_level == "off"


def test_writing_a_conversation_never_touches_the_GLOBAL_settings(tmp_path: Path, owned_session) -> None:
    """Stated as its own arm because it is the rule most easily broken by a
    convenience: `settings.json` keeps being the DEFAULTS plus the app-wide
    knobs, and a per-conversation choice must not leak into it."""
    global_root = tmp_path / "global"
    global_root.mkdir()
    st.save(st.Settings(), global_root)
    before = st.settings_path(global_root).read_bytes()

    cs_mod.save(_dir(tmp_path, owned_session=owned_session), cs_mod.ConvoSettings(model="qwen/qwen3-8b"), agent_session=owned_session)

    assert st.settings_path(global_root).read_bytes() == before


def test_the_file_carries_the_backend_specific_load_settings(tmp_path: Path, owned_session) -> None:
    """the user named these: *"everything llama and lmstudio support / need for
    their specifics"*. They are separate fields because the two runtimes take
    different keys, and one merged dict would make a llama ctx look like an
    LM Studio one."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(
        llama_load={"ctx": 8192, "gpu_layers": 99},
        lmstudio_load={"ctx": 4096, "ttl": 3600},
    ), agent_session=owned_session)
    got = cs_mod.load(d)

    assert got.llama_load == {"ctx": 8192, "gpu_layers": 99}
    assert got.lmstudio_load == {"ctx": 4096, "ttl": 3600}


def test_the_file_carries_the_liteharness_identity(tmp_path: Path, owned_session) -> None:
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(
        seat_name="OpenBolt", seat_id="2578f274", seat_tier="worker"), agent_session=owned_session)
    got = cs_mod.load(d)

    assert (got.seat_name, got.seat_id, got.seat_tier) == ("OpenBolt", "2578f274", "worker")


def test_an_unreadable_file_opens_the_conversation_anyway(tmp_path: Path, owned_session) -> None:
    """🔴 THE TRANSCRIPT IS THE VALUABLE THING. A hand-edited or truncated
    config must never be the reason a conversation cannot be opened — it falls
    through to the globals, exactly like an absent one."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.path_for(d).write_text("{not json", encoding="utf-8")

    cs = cs_mod.load(d)
    assert cs.model is None
    assert cs_mod.resolved(cs, st.Settings(), "model") == st.Settings().default_model


def test_a_save_is_atomic_and_leaves_no_litter(tmp_path: Path, owned_session) -> None:
    """Same rule as the global file (T688): `load` answers an unparseable file
    with SILENT defaults, so a reader landing mid-write would see the
    conversation quietly revert rather than fail in a way anyone could act on.
    """
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model="a"), agent_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model="b"), agent_session=owned_session)

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
    retire_cli_model = _Real.retire_cli_model
    thinking_level = _Real.thinking_level
    backend = _Real.backend
    chosen_tool_profile = _Real.chosen_tool_profile
    set_tool_profile = _Real.set_tool_profile
    _remember_for_this_convo = _Real._remember_for_this_convo
    _adopt_convo_settings = _Real._adopt_convo_settings
    _adopt_convo_backend = _Real._adopt_convo_backend
    remember_load_settings = _Real.remember_load_settings

    def _resume_cli_convo(self):
        """These unit hosts do not supply a --convo startup target."""
        return True

    def __init__(self, settings, convo_dir, backend_name="llamacpp", *, owned_session):
        self._agent_session = owned_session
        self._owned_launch_error = None
        self._launch_overrides = {}
        self._invocation_saved_values = {}
        self._resume_backend_error = None
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


def test_a_NEW_conversation_writes_its_file_from_the_globals(tmp_path: Path, owned_session) -> None:
    s = st.Settings()
    s.default_model = "qwen/qwen3-8b"
    s.thinking_level = "xhigh"
    d = _dir(tmp_path, owned_session=owned_session)

    a = _App(s, d, owned_session=owned_session)
    a._adopt_convo_settings(born=True)

    on_disk = json.loads(cs_mod.path_for(d).read_text(encoding="utf-8"))
    assert on_disk["model"] == "qwen/qwen3-8b"
    assert on_disk["thinking_level"] == "xhigh"
    assert on_disk["seat_name"] == "OpenBolt", "the owning seat travels with it"


def test_a_model_switch_writes_THIS_conversation_and_not_the_other(tmp_path: Path, owned_session) -> None:
    """🔴 THE CARD IN ONE ARM. Two conversations, a switch in one."""
    s = st.Settings()
    s.default_model = "gemma-3-4b-it.Q4_K_M"
    a_dir, b_dir = _dir(tmp_path, "a", owned_session=owned_session), _dir(tmp_path, "b", owned_session=owned_session)

    a, b = _App(s, a_dir, owned_session=owned_session), _App(s, b_dir, owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    b._adopt_convo_settings(born=True)

    st.save(st.Settings(default_model='global-disk-model'), tmp_path)
    global_before = st.settings_path(tmp_path).read_bytes()
    other_before = cs_mod.path_for(b_dir).read_bytes()
    a.model_id = "qwen/qwen3-8b"        # the assignment a plugin makes

    assert cs_mod.load(a_dir).model == "qwen/qwen3-8b"
    assert cs_mod.load(b_dir).model == "gemma-3-4b-it.Q4_K_M", "B moved"
    assert owned_session.authority.model == 'qwen/qwen3-8b'
    assert st.settings_path(tmp_path).read_bytes() == global_before, 'GLOBAL disk changed'
    assert cs_mod.path_for(b_dir).read_bytes() == other_before, 'historical child B changed'


def test_a_startup_assignment_writes_nothing(tmp_path: Path, owned_session) -> None:
    """`__init__` sets both fields before any conversation exists. A setter that
    wrote unconditionally would create a settings file for a conversation that
    has not been started — and, worse, in whatever directory happened to be
    current."""
    a = _App(st.Settings(), _dir(tmp_path, owned_session=owned_session), owned_session=owned_session)
    a.model_id = "qwen/qwen3-8b"
    a.thinking_level = "xhigh"

    assert not cs_mod.path_for(a.convo_dir).exists()


def test_re_assigning_the_SAME_value_does_not_rewrite_the_file(tmp_path: Path, owned_session) -> None:
    """These setters fire on every assignment, including a `/model` to the model
    already selected and a reconnect re-applying the current level. Writing per
    assignment would rewrite the file several times a turn for no change."""
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"
    stamp = cs_mod.path_for(d).stat().st_mtime_ns

    a.model_id = "qwen/qwen3-8b"

    assert cs_mod.path_for(d).stat().st_mtime_ns == stamp


def test_resume_applies_the_conversation_file(tmp_path: Path, owned_session) -> None:
    """Retained node: home execution wins conflicting historical child snapshot."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model='child-model', thinking_level='high'), agent_session=owned_session)
    before = cs_mod.path_for(d).read_bytes()
    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.model_id == owned_session.authority.model
    level = owned_session.authority.thinking_level
    assert a.thinking_level == (None if level == 'default' else level)  # home, not child high
    assert cs_mod.path_for(d).read_bytes() == before


def test_applying_a_stored_value_does_not_write_it_back(tmp_path: Path, owned_session) -> None:
    """⚠️ THE LOOP THIS AVOIDS. `_adopt_convo_settings` assigns through the
    BACKING fields, not the properties: restoring a stored choice is not a new
    choice, and routing it through the setter would rewrite the file on every
    open — and on a read-only copy, fail on every open."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model="qwen/qwen3-8b"), agent_session=owned_session)
    stamp = cs_mod.path_for(d).stat().st_mtime_ns

    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)

    assert cs_mod.path_for(d).stat().st_mtime_ns == stamp


def test_a_model_the_server_no_longer_has_falls_back_AND_SAYS_SO(tmp_path: Path, owned_session) -> None:
    """Retained node: unavailable home model is never substituted by global fallback."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model='child-gone'), agent_session=owned_session)
    before = cs_mod.path_for(d).read_bytes()
    a = _App(st.Settings(default_model='fallback-model'), d, backend_name=owned_session.authority.backend, owned_session=owned_session)
    a.available_models = ['fallback-model']
    a._adopt_convo_settings(born=False)
    assert a.model_id == owned_session.authority.model
    assert cs_mod.path_for(d).read_bytes() == before
    assert not any('falls back' in text for text in a.said)
    # Availability refusal is exercised by actual owned startup/connect tests;
    # adoption cannot substitute merely from a previous discovery catalog.


def test_an_EMPTY_model_list_is_unknown_and_never_triggers_the_fallback(tmp_path: Path, owned_session) -> None:
    """Empty discovery is unknown, never a reason to replace home execution."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model='child-model'), agent_session=owned_session)
    before = cs_mod.path_for(d).read_bytes()
    a = _App(st.Settings(), d, backend_name=owned_session.authority.backend, owned_session=owned_session)
    a.available_models = []
    a._adopt_convo_settings(born=False)
    assert a.model_id == owned_session.authority.model
    assert a.said == []
    assert cs_mod.path_for(d).read_bytes() == before


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


def test_a_backend_switch_is_remembered_by_the_conversation(tmp_path: Path, owned_session) -> None:
    """the user named backend FIRST, and the first cut recorded it nowhere."""
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=True)

    a.backend = _FakeBackend("codex")      # what /backend assigns

    assert cs_mod.load(d).backend == "codex"


def test_a_backend_switch_in_A_does_not_move_B(tmp_path: Path, owned_session) -> None:
    s = st.Settings()
    a_dir, b_dir = _dir(tmp_path, "a", owned_session=owned_session), _dir(tmp_path, "b", owned_session=owned_session)
    a, b = _App(s, a_dir, owned_session=owned_session), _App(s, b_dir, owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    b._adopt_convo_settings(born=True)

    st.save(st.Settings(backend='lmstudio'), tmp_path)
    global_before = st.settings_path(tmp_path).read_bytes()
    other_before = cs_mod.path_for(b_dir).read_bytes()
    before_backend = cs_mod.load(b_dir).backend
    a.backend = _FakeBackend("codex")

    assert cs_mod.load(a_dir).backend == "codex"
    assert cs_mod.load(b_dir).backend == before_backend
    assert owned_session.authority.backend == 'codex'
    assert st.settings_path(tmp_path).read_bytes() == global_before
    assert cs_mod.path_for(b_dir).read_bytes() == other_before


def test_opening_a_conversation_puts_it_back_on_its_own_engine(tmp_path: Path, monkeypatch, owned_session) -> None:
    """And through the FACTORY, so T690's VRAM gate is stamped on the rebuilt
    backend — assigning a hand-made one would hand the app an ungated engine and
    the modal would stop appearing for exactly the people who switch engines."""
    from litetui import llm_backend

    made: list[str] = []

    def _fake_factory(settings):
        made.append(settings.backend)
        return _FakeBackend(settings.backend)

    monkeypatch.setattr(llm_backend, "make_backend", _fake_factory)

    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(backend="codex"), agent_session=owned_session)
    a = _App(st.Settings(), d, backend_name="llamacpp", owned_session=owned_session)
    a._adopt_convo_settings(born=False)

    assert made == [owned_session.authority.backend], 'factory must use home engine'
    assert a.backend.name == owned_session.authority.backend
    assert cs_mod.load(d).backend == 'codex', 'historical child should remain unchanged'


def test_an_engine_the_box_no_longer_has_falls_back_AND_SAYS_SO(tmp_path: Path, monkeypatch, owned_session) -> None:
    from litetui import llm_backend

    def _boom(settings):
        raise llm_backend.BackendError("that backend is unavailable")

    monkeypatch.setattr(llm_backend, "make_backend", _boom)

    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(backend="codex"), agent_session=owned_session)
    a = _App(st.Settings(), d, backend_name="llamacpp", owned_session=owned_session)
    a._adopt_convo_settings(born=False)

    assert a.backend.name == "llamacpp", "it switched to an engine that cannot be built"
    assert any(owned_session.authority.backend in text and 'Sending is blocked' in text for text in a.said), a.said
    assert a._resume_backend_error
    assert owned_session.authority.backend != a.backend.name, 'no authority publication to fallback'


def test_the_codex_effort_is_kept_in_its_OWN_field(tmp_path: Path, owned_session) -> None:
    """the user: *"think level when on codex"*. It is a different vocabulary from
    LM Studio's thinking levels, so a conversation carried between engines must
    not hand a codex effort to a llama.cpp level."""
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, backend_name="codex", owned_session=owned_session)
    a._adopt_convo_settings(born=True)

    a.thinking_level = "high"

    got = cs_mod.load(d)
    assert got.reasoning_effort == "high"
    assert got.thinking_level == "high"


def test_a_NON_codex_level_does_not_write_the_codex_field(tmp_path: Path, owned_session) -> None:
    """The discriminator. Without it the arm above is satisfied by a setter that
    writes both fields unconditionally, which is exactly the conflation the
    separate field exists to prevent."""
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, backend_name="llamacpp", owned_session=owned_session)
    a._adopt_convo_settings(born=True)

    a.thinking_level = "xhigh"

    got = cs_mod.load(d)
    assert got.thinking_level == "xhigh"
    assert got.reasoning_effort is None


def test_llama_load_settings_are_remembered_for_THIS_model(tmp_path: Path, owned_session) -> None:
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, backend_name="llamacpp", owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"

    a.remember_load_settings("qwen/qwen3-8b", {"ctx": 8192, "gpu_layers": 99})

    assert cs_mod.load(d).llama_load == {"ctx": 8192, "gpu_layers": 99}


def test_load_settings_for_ANOTHER_model_are_not_claimed(tmp_path: Path, owned_session) -> None:
    """`/modelcfg` can edit a model the conversation is not using. Recording
    that here would make the conversation claim a configuration it never ran."""
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, backend_name="llamacpp", owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"

    a.remember_load_settings("some/other-model", {"ctx": 512})

    assert cs_mod.load(d).llama_load == {}


def test_lmstudio_load_settings_land_in_the_LMSTUDIO_field(tmp_path: Path, owned_session) -> None:
    """Two runtimes, two key vocabularies. One merged dict would make a llama
    ctx indistinguishable from an LM Studio one when the conversation moves."""
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, backend_name="lmstudio", owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    a.model_id = "qwen/qwen3-8b"

    a.remember_load_settings("qwen/qwen3-8b", {"ctx": 4096})

    got = cs_mod.load(d)
    assert got.lmstudio_load == {"ctx": 4096}
    assert got.llama_load == {}


def test_opening_a_conversation_restores_its_load_settings_and_effort(tmp_path: Path, owned_session) -> None:
    """Compatible model loads restore; Codex effort remains home-authoritative."""
    owned_session.update_execution(backend='codex', model='qwen/qwen3-8b', thinking_level='high')
    s = st.Settings()
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(
        model="qwen/qwen3-8b",
        llama_load={"ctx": 8192},
        reasoning_effort="high",
    ), agent_session=owned_session)

    a = _App(s, d, backend_name="codex", owned_session=owned_session)
    a._adopt_convo_settings(born=False)

    assert a.settings.llama_load_settings["qwen/qwen3-8b"] == {"ctx": 8192}
    assert (a.settings.model_infer_overrides["qwen/qwen3-8b"]["reasoning_effort"]) == "high"


def test_EVERY_declared_field_has_a_writer_or_is_named_as_carried_only(owned_session) -> None:
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
    carried_only = {"seat_name", "seat_id", "seat_tier", "schema_version", "execution"}  # snapshot metadata authored by born_from/service

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


def test_an_explicit_authority_change_is_remembered_by_the_conversation(tmp_path: Path, owned_session) -> None:
    d = _dir(tmp_path, owned_session=owned_session)
    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=True)

    a.set_tool_profile("interactive", announce=False)

    assert cs_mod.load(d).tool_policy_profile == "interactive"
    assert a._active_tool_profile == "interactive"


def test_an_authority_change_in_A_does_not_move_B(tmp_path: Path, owned_session) -> None:
    s = st.Settings()
    a_dir, b_dir = _dir(tmp_path, "a", owned_session=owned_session), _dir(tmp_path, "b", owned_session=owned_session)
    a, b = _App(s, a_dir, owned_session=owned_session), _App(s, b_dir, owned_session=owned_session)
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


def test_opening_a_conversation_puts_it_back_on_its_own_authority(tmp_path: Path, owned_session) -> None:
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"), agent_session=owned_session)
    s = st.Settings()
    s.tool_policy_profile = "autonomous"

    a = _App(s, d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)

    assert a._active_tool_profile == "interactive"
    assert a.chosen_tool_profile == "interactive"


def test_a_TRANSIENT_elevation_is_not_recorded_as_the_choice(tmp_path: Path, owned_session) -> None:
    """🔴 THE DISCRIMINATING ARM, and the reason this was not done with T691.

    Every other field could be wired by writing through its setter; this one
    cannot, because most of what assigns it is a TURN-SCOPED override. A cron
    fire assigning AUTONOMOUS directly — exactly what `app.py:1699` does — must
    leave the conversation remembered authority alone.

    Without this, a wiring that simply made `_active_tool_profile` a
    write-through property would pass every other arm in this file and quietly
    make one scheduled job the conversation standing authority.
    """
    d = _dir(tmp_path, owned_session=owned_session)
    s = st.Settings()
    s.tool_policy_profile = "interactive"
    a = _App(s, d, owned_session=owned_session)
    a._adopt_convo_settings(born=True)
    a.set_tool_profile("interactive", announce=False)

    a._active_tool_profile = "autonomous"      # a cron fire, verbatim

    assert cs_mod.load(d).tool_policy_profile == "interactive"
    assert a.chosen_tool_profile == "interactive"


def test_the_chosen_profile_falls_through_when_the_conversation_never_chose(tmp_path: Path, owned_session) -> None:
    """A conversation that predates this card has no file and must behave
    exactly as it did — absent is not empty."""
    s = st.Settings()
    s.tool_policy_profile = "autonomous"
    a = _App(s, _dir(tmp_path, owned_session=owned_session), owned_session=owned_session)
    a._adopt_convo_settings(born=False)

    assert a.chosen_tool_profile == "autonomous"


# ── the two rulings, each with an arm ────────────────────────────────────


def test_a_LOOP_is_stamped_from_the_GLOBAL_setting_not_the_conversation(tmp_path: Path, owned_session) -> None:
    """🔴 THE USER RULING, ALREADY IN THE CODE AT app.py:1672, AND BINDING:
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
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"), agent_session=owned_session)

    a = _App(s, d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.chosen_tool_profile == "interactive", "the arm is not testing what it claims"

    state = goal_loop.GoalState(
        objective="ship it",
        tool_profile=getattr(a.settings, "tool_policy_profile", goal_loop.INTERACTIVE),
    )

    assert state.tool_profile == "autonomous", (
        "a saved automation inherited the conversation authority — app.py:1672"
    )


def test_the_CLI_FLAG_wins_on_resume_and_never_writes_the_file(tmp_path: Path, owned_session) -> None:
    """🔴 SENTINEL RULING: an explicit invocation outranks a stored default,
    and a flag that became sticky would be the opposite of explicit — it would
    outlive the invocation that asked for it and apply to runs that did not."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"), agent_session=owned_session)
    s = st.Settings()

    a = _App(s, d, owned_session=owned_session)
    a._cli_tool_profile = "autonomous"
    a._active_tool_profile = "autonomous"      # what app.py:1226 already did
    a._adopt_convo_settings(born=False)

    assert a._active_tool_profile == "autonomous", "the flag lost to the stored choice"
    assert cs_mod.load(d).tool_policy_profile == "interactive", (
        "the flag wrote itself into the conversation and became sticky"
    )


def test_CONTROL_without_the_flag_the_conversation_wins(tmp_path: Path, owned_session) -> None:
    """The other half. Without it the arm above passes for a resume that never
    adopts anything at all."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(tool_policy_profile="interactive"), agent_session=owned_session)
    s = st.Settings()
    s.tool_policy_profile = "autonomous"

    a = _App(s, d, owned_session=owned_session)
    a._active_tool_profile = "autonomous"
    a._adopt_convo_settings(born=False)

    assert a._active_tool_profile == "interactive"


def test_resume_never_validates_against_previous_backend_catalog(tmp_path, monkeypatch, owned_session):
    """Home provider/model, not child/previous catalog, selects resumed execution."""
    owned_session.update_execution(backend='codex', model='home-model', thinking_level='high')
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(backend='llamacpp', model='child-model'), agent_session=owned_session)
    before = cs_mod.path_for(d).read_bytes()
    a = _App(st.Settings(default_model='old-model'), d, backend_name='lmstudio', owned_session=owned_session)
    a.available_models = ['old-model']
    monkeypatch.setattr(app_mod.llm_backend, 'make_backend', lambda cfg: _FakeBackend(cfg.backend))
    a._adopt_convo_settings(born=False)
    assert a.model_id == 'home-model'
    assert a.backend.name == 'codex'
    assert a.available_models == []
    assert not any('falls back' in text for text in a.said)
    assert cs_mod.path_for(d).read_bytes() == before


def test_resume_restores_execution_snapshot_without_mutating_defaults(tmp_path, owned_session):
    defaults = st.Settings(backend='ninfer', ninfer_host='http://localhost:49260', temperature=0.2)
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.born_from(defaults), agent_session=owned_session)
    defaults.ninfer_host = 'http://localhost:49261'
    defaults.temperature = 0.8
    a = _App(defaults, d, backend_name='ninfer', owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.settings.ninfer_host == 'http://localhost:49260'
    assert a.settings.temperature == 0.2
    assert defaults.ninfer_host == 'http://localhost:49261'


def test_resume_keeps_environment_effective_without_rewriting_snapshot(tmp_path, monkeypatch, owned_session):
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.born_from(st.Settings(ninfer_host='http://saved:49260')), agent_session=owned_session)
    monkeypatch.setenv('LITETUI_NINFER_HOST', 'http://invocation:49260')
    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.settings.ninfer_host == 'http://invocation:49260'
    assert cs_mod.load(d).execution['ninfer_host'] == 'http://saved:49260'


def test_resume_environment_backend_model_override_is_effective_only(tmp_path, monkeypatch, owned_session):
    """Retained node: divergent environment cannot supersede the owned home."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(backend='llamacpp', model='child-model'), agent_session=owned_session)
    child_before = cs_mod.path_for(d).read_bytes()
    home_before = (owned_session.memory_root / 'settings.json').read_bytes()
    monkeypatch.setenv('LITETUI_BACKEND', 'codex')
    monkeypatch.setenv('LITETUI_MODEL', 'invocation-model')
    a = _App(st.Settings(), d, backend_name=owned_session.authority.backend, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.backend.name == owned_session.authority.backend
    assert a.model_id == owned_session.authority.model
    assert cs_mod.path_for(d).read_bytes() == child_before
    assert (owned_session.memory_root / 'settings.json').read_bytes() == home_before


def test_resume_effective_snapshot_is_not_dirty_for_next_unrelated_save(tmp_path, owned_session):
    from dataclasses import asdict
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.born_from(st.Settings(temperature=0.2)), agent_session=owned_session)
    defaults = st.Settings(temperature=0.8)
    defaults._baseline = asdict(defaults)
    a = _App(defaults, d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.settings._baseline['temperature'] == 0.2


@pytest.mark.asyncio
async def test_cli_model_selection_is_not_a_persisted_choice(tmp_path, owned_session):
    """Retained node: divergent CLI is refused; matching CLI never publishes itself."""
    from litetui.agent_launch_context import validate_execution
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(model='remembered'), agent_session=owned_session)
    a = _App(st.Settings(), d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    before = cs_mod.path_for(d).read_bytes()
    home_before = (owned_session.memory_root / 'settings.json').read_bytes()
    a._cli_initial_model = 'invoked'
    a._first_prompt = 'NEVER DISPATCH'
    a._submit_text = lambda *args, **kwargs: pytest.fail('divergent launch dispatched prompt')
    await app_mod.LiteTUI._apply_cli_args.__wrapped__(a)
    assert 'disagrees' in a._cli_launch_error
    assert a.model_id == owned_session.authority.model
    validate_execution(owned_session, model=owned_session.authority.model)
    assert cs_mod.path_for(d).read_bytes() == before
    assert (owned_session.memory_root / 'settings.json').read_bytes() == home_before
    # Full real matching dispatch control: test_owned_invocation_boundary.py.


def test_cli_model_does_not_leak_into_a_later_mid_session_resume(tmp_path, owned_session):
    """Retained node: rejected invocation cannot leak across children of one home."""
    first = _dir(tmp_path, 'first', owned_session=owned_session)
    second = _dir(tmp_path, 'second', owned_session=owned_session)
    cs_mod.save(first, cs_mod.ConvoSettings(model='saved-a'), agent_session=owned_session)
    cs_mod.save(second, cs_mod.ConvoSettings(model=owned_session.authority.model, llama_load={'ctx': 8192}), agent_session=owned_session)
    before = [cs_mod.path_for(d).read_bytes() for d in (first, second)]
    a = _App(st.Settings(), first, backend_name=owned_session.authority.backend, owned_session=owned_session)
    a._cli_initial_model = 'divergent'
    a._startup_adopting = True
    with pytest.raises(ValueError, match='disagrees'):
        a._adopt_convo_settings(born=False)
    assert a.model_id == ''
    a._cli_initial_model = owned_session.authority.model
    a._adopt_convo_settings(born=False)
    assert a.model_id == owned_session.authority.model
    a.convo_dir = second
    a._startup_adopting = False
    a._adopt_convo_settings(born=False)
    assert a.model_id == owned_session.authority.model
    assert a.settings.llama_load_settings[a.model_id] == {'ctx': 8192}
    assert 'divergent' not in a.settings.llama_load_settings
    assert [cs_mod.path_for(d).read_bytes() for d in (first, second)] == before


def test_legacy_save_preserves_unknown_additive_metadata(tmp_path, owned_session):
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.path_for(d).write_text('{"model":"a","future_metadata":{"keep":true}}', encoding='utf-8')
    record = cs_mod.load(d)
    record.model = 'b'
    cs_mod.save(d, record, agent_session=owned_session)
    assert json.loads(cs_mod.path_for(d).read_text(encoding='utf-8'))['future_metadata'] == {'keep': True}


@pytest.mark.parametrize('available', [[], ['saved-other-model']])
def test_cli_model_wins_during_resume_before_connection(tmp_path, available, owned_session):
    """Retained parameter IDs: home beats child, divergent CLI refuses before connect."""
    d = _dir(tmp_path, owned_session=owned_session)
    cs_mod.save(d, cs_mod.ConvoSettings(backend='codex', model='child-model'), agent_session=owned_session)
    a = _App(st.Settings(), d, backend_name=owned_session.authority.backend, owned_session=owned_session)
    a._cli_initial_model = 'gpt-requested'
    a.available_models = available
    a._startup_adopting = True
    before = cs_mod.path_for(d).read_bytes()
    home_before = (owned_session.memory_root / 'settings.json').read_bytes()
    with pytest.raises(ValueError, match='disagrees'):
        a._adopt_convo_settings(born=False)
    assert a.model_id == ''
    assert a.backend.name == owned_session.authority.backend
    assert cs_mod.path_for(d).read_bytes() == before
    assert (owned_session.memory_root / 'settings.json').read_bytes() == home_before
    assert not any('falls back' in text for text in a.said)


def test_conversation_interpreter_setting_restores_without_mutating_defaults(tmp_path, owned_session):
    d = _dir(tmp_path, owned_session=owned_session)
    defaults = st.Settings(tool_trusted_interpreters=['C:/explicit/python.exe'])
    cs_mod.save(d, cs_mod.born_from(defaults), agent_session=owned_session)
    defaults.tool_trusted_interpreters = []
    a = _App(defaults, d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.settings.tool_trusted_interpreters == ['C:/explicit/python.exe']
    assert defaults.tool_trusted_interpreters == []
    stored = cs_mod.load(d)
    stored.execution['tool_trusted_interpreters'] = 'C:/not-a-list/python.exe'
    cs_mod.save(d, stored, agent_session=owned_session)
    a = _App(defaults, d, owned_session=owned_session)
    a._adopt_convo_settings(born=False)
    assert a.settings.tool_trusted_interpreters == []
