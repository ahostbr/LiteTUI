"""T691 — each conversation carries its own model configuration.

Ryan (a-62edbbe0): *"per convo settings files json that save backend model
liteharness-info think level ... and everything llama and lmstudio support"*.

⚠️ EVERY ARM USES A TEMP DIRECTORY. `.convos/` is live on this box with hundreds
of real conversations in it.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    _remember_for_this_convo = _Real._remember_for_this_convo
    _adopt_convo_settings = _Real._adopt_convo_settings

    def __init__(self, settings, convo_dir):
        self.settings = settings
        self.convo_dir = convo_dir
        self.seat = _FakeSeat()
        self.available_models: list[str] = []
        self.said: list[str] = []
        self._convo_settings = None
        self._model_id = ""
        self._thinking_level = None

    def _system(self, text):
        self.said.append(text)


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
