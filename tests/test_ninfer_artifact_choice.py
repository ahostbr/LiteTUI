"""On NInfer the model IS the artifact, and until T860 nothing could pick one.

🔴 WHAT RYAN HIT. Engine down, and every model command answered with a remedy
that state had removed:

    /model    -> "No models discovered — try /reconnect"   (reconnects to nothing)
    /load     -> "No model selected — /model first"        (no list can exist)
    /engine start -> "no NInfer artifact chosen — set ninfer_artifact ..."

The last one is the real wall. `ninfer_artifact()` auto-selects ONLY when
exactly one `.ninfer` is on disk — "the wrong 22 GB file is a long wait that
ends in the wrong model". He had THREE, because a conversion run wrote a third
that evening. So the guard stopped auto-selecting, correctly, hours before he
tried to start anything.

    THE GUARD IS RIGHT. WHAT WAS WRONG IS THAT THE EXIT IT LEAVES HAD NO DOOR
    IN THE SURFACE HE WAS USING. The only way to choose was typing a path into
    /settings — and no message said so.

⬜ AND THE SHAPE, because this is the second instance in one evening. T858 was
a handler that NEEDED the missing thing (the "no engine" path called `host()`,
which raises when there is no engine). T860 is a handler that RECOMMENDS the
missing thing. One root: the message was written for the state where the
backend works.

🔴 NO ENGINE IS EVER STARTED BY THIS FILE. Listing artifacts is file I/O;
starting one is a VRAM load and needs Ryan's per-load approval. Every arm below
builds fake `.ninfer` files in `tmp_path` and never touches the real models
directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from litetui import ninfer_engine
from litetui.plugins import model_switch
from litetui.settings import Settings


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    """Point the artifact directory at a throwaway tree and return a factory."""
    root = tmp_path / "ninfer-models"
    root.mkdir()
    monkeypatch.setattr(ninfer_engine, "artifacts_dir", lambda: root)

    def make(*names: str) -> list[Path]:
        for n in names:
            (root / n).write_bytes(b"not a real artifact")
        return sorted(root.glob("*.ninfer"))

    make.root = root
    return make


class _App:
    """The command surface, nothing more. `pick` is captured, not rendered."""

    def __init__(self, settings=None):
        self.settings = settings or Settings(ninfer_artifact="")
        self.backend = _NInferish()
        self.said: list[str] = []
        self.picked: list[tuple] = []
        self.available_models: list[str] = []
        self.model_rows: dict = {}
        self.model_id = ""
        self._rpc = False

    def system_message(self, msg, *a, **k):
        self.said.append(str(msg))

    def _on_settings_saved(self, new):
        self.settings = new


class _NInferish:
    name = "ninfer"
    remote = False

    def empty_state_hint(self) -> str:
        from litetui.ninfer_backend import NInferBackend
        return NInferBackend.empty_state_hint(self)


@pytest.fixture(autouse=True)
def _capture_pick(monkeypatch):
    """`pick` opens a modal screen; here it records what it was handed."""
    def fake(app, title, rows, on_pick, current=None):
        app.picked.append((title, rows, on_pick, current))
    monkeypatch.setattr(model_switch, "pick", fake)


# ── the three populations ────────────────────────────────────────────────────


def test_zero_artifacts_says_where_they_come_from(artifacts):
    """⬜ Nothing to choose is not the same as a broken command: it names the
    directory and who fills it, rather than offering an empty list."""
    artifacts()
    app = _App()
    model_switch._cmd_model(app, "model", "")
    assert app.picked == [], "an empty picker is worse than a sentence"
    assert any("ninfer-models" in s for s in app.said), app.said
    assert any("Model Hub" in s for s in app.said), app.said


def test_exactly_one_artifact_still_auto_selects(artifacts):
    """🔴 THE GUARD MUST SURVIVE THE FIX. One file on disk has always meant "no
    question to ask", and T860 must not turn that into a prompt — that would be
    the modal-fatigue defect wearing a usability label."""
    made = artifacts("solo.ninfer")
    assert ninfer_engine.ninfer_artifact(Settings(ninfer_artifact="")) == made[0]


def test_three_artifacts_offer_a_choice_and_picking_sets_the_setting(artifacts):
    """🔴 RYAN'S EXACT STATE. Three files, so `ninfer_artifact()` refuses to
    guess — and now `/model` asks."""
    made = artifacts("a.ninfer", "b.ninfer", "c.ninfer")
    assert ninfer_engine.ninfer_artifact(Settings(ninfer_artifact="")) is None

    app = _App()
    model_switch._cmd_model(app, "model", "")
    assert len(app.picked) == 1, app.said
    _title, rows, on_pick, _current = app.picked[0]
    assert sorted(r[0] for r in rows) == [str(p) for p in made]

    on_pick(str(made[1]))
    assert app.settings.ninfer_artifact == str(made[1])
    # The next step is named, and it is the one that actually works.
    assert any("/engine start" in s for s in app.said), app.said


def test_cancelling_the_picker_changes_nothing(artifacts):
    """⬜ The control for the arm above: a fix that wrote the setting
    unconditionally would pass it."""
    artifacts("a.ninfer", "b.ninfer")
    app = _App()
    model_switch._cmd_model(app, "model", "")
    app.picked[0][2](None)
    assert app.settings.ninfer_artifact == ""


# ── the remedy comes from the backend ────────────────────────────────────────


def test_the_ninfer_empty_state_names_commands_that_work():
    """🔴 THE TOKENS, NOT THE PROSE. Pinning a sentence would fail the next time
    someone improves the wording; what must not change is WHICH COMMANDS are
    offered."""
    hint = model_switch._empty_state_hint(_App())
    assert "/model" in hint
    assert "/engine start" in hint
    assert "/reconnect" not in hint


def test_the_other_backends_words_are_untouched():
    """🔴 THE HALF THAT STOPS THIS BEING A FIND-AND-REPLACE. A backend with no
    `empty_state_hint` keeps exactly today's remedy."""
    class _Plain:
        name = "llamacpp"
        remote = False

    app = _App()
    app.backend = _Plain()
    assert model_switch._empty_state_hint(app) == "try /reconnect"


def test_a_backend_whose_hint_raises_does_not_break_the_command():
    """⬜ A hint is a message, and a message must never be the thing that fails.
    Learned on T858, where the log line took the turn down with it."""
    class _Angry:
        name = "boom"
        remote = False

        def empty_state_hint(self):
            raise RuntimeError("no")

    app = _App()
    app.backend = _Angry()
    assert model_switch._empty_state_hint(app) == "try /reconnect"


# ── /load refuses rather than re-words ───────────────────────────────────────


def test_load_refuses_on_ninfer_and_says_why():
    """⬜ `/load` asks a server to bring a model into memory. NInfer has no
    `/models/load` at all, so a re-worded error would still imply the verb
    exists here."""
    app = _App()
    model_switch._cmd_load(app, "load", "")
    assert app.picked == []
    joined = " ".join(app.said)
    assert "one artifact per process" in joined
    assert "/model" in joined and "/engine start" in joined
