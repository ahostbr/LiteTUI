"""A projector sitting beside a model is paired with it. T231.

Ryan, shown a vision card that could not be captured: "might b a bug with our
lamma setup ... not loading the vision gguf i think ... like lmstudio does for
us."

He was right, and the projector was never far away. `_skip_gguf` drops every
`*mmproj*.gguf` from model discovery — correct, they are not chat models — and
nothing paired one back up, so a vision-capable model loaded text-only unless
somebody typed the path by hand. Measured over ~/.lmstudio/models 2026-09-03:
nine directories hold a projector, eight of those hold exactly one model beside
it, and exactly ONE pairing existed in settings.json.

🔴 THE SCOPE IS THE MODEL'S OWN DIRECTORY, AND THAT IS NOT FUSSINESS. Two vendor
directories on this box share the basename `Qwen3.8-27B-GGUF`
(`unsloth/…` and `lmstudio-community/…`), each with its own model and its own
projector. Pair by directory and both are right; pair by any global list and the
nearest match by sort order is a coin toss. A wrong projector does not fail
loudly — it changes what the model can see.

⚠️ AND MY FIRST MEASUREMENT OF THAT WAS WRONG, WHICH IS WHY IT IS ASSERTED HERE.
I printed `parent.name`, saw two projectors under "Qwen3.8-27B-GGUF", and
reported one directory holding two. Re-derived by FULL PATH: zero directories on
this box hold two. The exactly-one rule survives on its own merits — an
ambiguous directory cannot be resolved by position — but the example I gave for
it did not exist, so the ambiguous case is built here rather than cited.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import llm_backend
from litetui.llm_backend import (
    NO_PROJECTOR, ModelRow, is_no_projector, sibling_mmproj, write_preset_ini,
)


def _gguf(p: Path, size: int = 20 * 1024 * 1024) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    return p


class _Settings:
    def __init__(self, load=None):
        self.llama_load_settings = load or {}


# ── the rule ────────────────────────────────────────────────────────────────

def test_exactly_one_projector_beside_the_model_is_paired(tmp_path):
    model = _gguf(tmp_path / "vendor-a" / "Model-Q4.gguf")
    proj = _gguf(tmp_path / "vendor-a" / "mmproj-F16.gguf")
    auto, found = sibling_mmproj(model)
    assert auto == str(proj)
    assert found == [str(proj)]


def test_two_projectors_pair_NOTHING_and_report_both(tmp_path):
    """The ambiguous case is CONSTRUCTED, not cited — see the module docstring."""
    model = _gguf(tmp_path / "vendor-b" / "Model-Q4.gguf")
    a = _gguf(tmp_path / "vendor-b" / "mmproj-F16.gguf")
    b = _gguf(tmp_path / "vendor-b" / "mmproj-BF16.gguf")
    auto, found = sibling_mmproj(model)
    assert auto is None, "two candidates were resolved to one by position"
    assert set(found) == {str(a), str(b)}, (
        "the caller cannot explain the refusal without both names"
    )


def test_no_projector_and_no_path_are_both_quiet(tmp_path):
    lonely = _gguf(tmp_path / "vendor-c" / "Model-Q4.gguf")
    assert sibling_mmproj(lonely) == (None, [])
    assert sibling_mmproj(None) == (None, [])


def test_two_directories_sharing_a_basename_pair_INDEPENDENTLY(tmp_path):
    """🔴 THE CASE THAT MADE ME MEASURE BY FULL PATH.

    `unsloth/Qwen3.8-27B-GGUF` and `lmstudio-community/Qwen3.8-27B-GGUF` both
    exist on this box. Anything that keys on the directory NAME sees one folder
    with two projectors and two models; keyed on the PATH they are two clean
    pairs. This is the arm that fails if the scope ever loosens to a name.
    """
    m1 = _gguf(tmp_path / "unsloth" / "Same-Name-GGUF" / "UD-Q4.gguf")
    p1 = _gguf(tmp_path / "unsloth" / "Same-Name-GGUF" / "mmproj-F16.gguf")
    m2 = _gguf(tmp_path / "community" / "Same-Name-GGUF" / "Q4.gguf")
    p2 = _gguf(tmp_path / "community" / "Same-Name-GGUF" / "mmproj-BF16.gguf")
    assert sibling_mmproj(m1)[0] == str(p1)
    assert sibling_mmproj(m2)[0] == str(p2)


# ── the ini ─────────────────────────────────────────────────────────────────

def test_the_generated_ini_carries_the_auto_paired_projector(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    proj = _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings(),
        dest=tmp_path / "out.ini",
    )
    text = dest.read_text(encoding="utf-8")
    # The EXACT line, not "mmproj appears somewhere": a substring check would be
    # satisfied by the word turning up in a comment or a path, which is the
    # assertion-satisfiable-by-the-wrong-answer shape this repo keeps finding.
    assert f"mmproj = {proj}" in text, text


def test_an_explicit_projector_WINS_over_the_sibling(tmp_path, monkeypatch):
    """The auto-pair fills a gap; it never overrides a choice.

    ⚠️ ABSENT IS THE TEST FOR "NOT CHOSEN", not None — `_collect_group` POPS a
    cleared text field rather than storing a null (verified against the live
    settings.json: `grep -c '"mmproj": null'` -> 0). So a stored key, whatever
    its value, is the user speaking.
    """
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    chosen = _gguf(tmp_path / "elsewhere" / "mmproj-CHOSEN.gguf")
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings({"model-q4": {"mmproj": str(chosen)}}),
        dest=tmp_path / "out.ini",
    )
    text = dest.read_text(encoding="utf-8")
    assert "mmproj-CHOSEN.gguf" in text
    assert "mmproj-F16.gguf" not in text, "the guess overrode an explicit path"


def test_an_ambiguous_directory_writes_NO_projector(tmp_path, monkeypatch):
    """Both polarities of the rule reach the ini, not just the happy one."""
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    _gguf(tmp_path / "v" / "mmproj-BF16.gguf")
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings(),
        dest=tmp_path / "out.ini",
    )
    assert "mmproj" not in dest.read_text(encoding="utf-8"), (
        "an ambiguous directory was guessed at"
    )


# ── the announcement ────────────────────────────────────────────────────────

def _app_with(monkeypatch, rows, load_cfg=None):
    from litetui.plugins import model_switch

    class _App:
        def __init__(self):
            self.settings = _Settings(load_cfg or {})
            self.said: list[str] = []

        def system_message(self, text):
            self.said.append(str(text))

    monkeypatch.setattr(llm_backend, "scan_models", lambda s: rows)
    return _App(), model_switch


def test_a_paired_projector_is_NAMED_when_the_model_loads(tmp_path, monkeypatch):
    """🔴 ANNOUNCED, NOT SILENT — Ryan's ruling.

    A guess that changes a model's modalities must not be invisible: with a
    projector the model reads the image, without one it answers as though the
    image were not there, and BOTH look identical at the prompt.
    """
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    rows = [ModelRow(key="m", path=str(model), source="custom")]
    app, model_switch = _app_with(monkeypatch, rows)
    model_switch._say_projector(app, "m")
    assert any("mmproj-F16.gguf" in s for s in app.said), app.said


def test_an_ambiguous_directory_says_WHY_and_lists_them(tmp_path, monkeypatch):
    """The count and the names are the reason, so they are IN the line.

    "No projector" alone sends someone looking for a missing file; this sends
    them to the one decision that resolves it.
    """
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    _gguf(tmp_path / "v" / "mmproj-BF16.gguf")
    rows = [ModelRow(key="m", path=str(model), source="custom")]
    app, model_switch = _app_with(monkeypatch, rows)
    model_switch._say_projector(app, "m")
    joined = " ".join(app.said)
    assert "2 projectors" in joined, joined
    assert "mmproj-F16.gguf" in joined and "mmproj-BF16.gguf" in joined, joined
    assert "text only" in joined.lower(), joined


def test_an_explicit_choice_is_NOT_announced(tmp_path, monkeypatch):
    """Silence has to be asserted too, or "announce" degenerates into chatter.

    A line every load about a path the user typed is noise, and noise is what
    made two unrelated tests fail earlier today when T239 added one.
    """
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    chosen = _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    rows = [ModelRow(key="m", path=str(model), source="custom")]
    app, model_switch = _app_with(monkeypatch, rows, {"m": {"mmproj": str(chosen)}})
    model_switch._say_projector(app, "m")
    assert app.said == [], f"an explicit setting was announced as a guess: {app.said}"


def test_a_model_with_no_projector_says_nothing(tmp_path, monkeypatch):
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    rows = [ModelRow(key="m", path=str(model), source="custom")]
    app, model_switch = _app_with(monkeypatch, rows)
    model_switch._say_projector(app, "m")
    assert app.said == [], f"a text-only model produced vision noise: {app.said}"


# ── T245: an EXPLICIT "no projector", distinct from an absent key ────────────
#
# 🔴 THE WHOLE FEATURE IS THE DISTINCTION, so every arm below is paired with the
# ABSENT case it must not collapse into. T231 had two answers — absent (guess)
# and a path (theirs); clearing the field removed the key, which is exactly what
# re-enabled the guess, so there was no way to load a vision model text-only ON
# PURPOSE. These arms fail if `none` ever starts behaving like absent, and the
# absent arms above fail if it goes the other way.


def test_the_sentinel_recogniser_accepts_what_a_human_types_and_nothing_else():
    """One spelling, compared the way a typed field arrives.

    Case and surrounding space are a human's, not a decision — but a SET of
    spellings would be several chances for the field's help line and the parser
    to disagree about which the product means.
    """
    assert is_no_projector(NO_PROJECTOR)
    assert is_no_projector("  NONE  ")
    assert is_no_projector("None")
    assert not is_no_projector("")
    assert not is_no_projector(None)
    assert not is_no_projector("off"), "a second spelling is a second contract"
    assert not is_no_projector("/models/v/mmproj-F16.gguf")


def test_an_explicit_none_writes_NO_projector_even_with_a_sibling(tmp_path, monkeypatch):
    """🔴 THE CASE THAT DID NOT EXIST BEFORE: a projector IS sitting beside the
    model, and the ini must still carry none.

    The sibling is the discriminator. Without it this arm would also pass on the
    old code, which wrote nothing simply because there was nothing to write.
    """
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    _gguf(tmp_path / "v" / "mmproj-F16.gguf")          # the guess this refuses
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings({"model-q4": {"mmproj": NO_PROJECTOR}}),
        dest=tmp_path / "out.ini",
    )
    text = dest.read_text(encoding="utf-8")
    assert "mmproj" not in text, (
        "the ini carries a projector for a model told explicitly to have none:\n"
        + text
    )
    assert "model = " in text, "the model itself stopped being written"


def test_the_sentinel_is_DROPPED_not_emitted_as_a_filename(tmp_path, monkeypatch):
    """`mmproj = none` would reach llama.cpp as a PATH and fail at load with a
    message about a missing file — the opposite of the user's intent, and a
    failure they would read as a bug in the picker."""
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings({"model-q4": {"mmproj": NO_PROJECTOR}}),
        dest=tmp_path / "out.ini",
    )
    assert "none" not in dest.read_text(encoding="utf-8").lower()


def test_an_explicit_path_still_WINS_over_the_sentinel_logic(tmp_path, monkeypatch):
    """T231's contract, re-proved on the branch that now sits beside it."""
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    chosen = _gguf(tmp_path / "elsewhere" / "mmproj-CHOSEN.gguf")
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings({"model-q4": {"mmproj": str(chosen)}}),
        dest=tmp_path / "out.ini",
    )
    assert f"mmproj = {chosen}" in dest.read_text(encoding="utf-8")


def test_loading_with_none_SAYS_SO(tmp_path, monkeypatch):
    """⬜ THE ONE EXPLICIT SETTING THAT STILL SPEAKS.

    Every other typed value is silent, for the reason
    `test_an_explicit_choice_is_NOT_announced` gives. This one is not, because
    "a vision model is loading TEXT-ONLY" is the same modality change the
    auto-pair line exists for, seen from the other side: if the guess must not
    be silent, neither must the refusal of it.
    """
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    rows = [ModelRow(key="m", path=str(model), source="custom")]
    app, model_switch = _app_with(monkeypatch, rows, {"m": {"mmproj": NO_PROJECTOR}})
    model_switch._say_projector(app, "m")
    joined = " ".join(app.said)
    assert "none" in joined.lower(), joined
    assert "your choice" in joined.lower(), joined
    assert "mmproj-F16.gguf" not in joined, (
        "the sibling was named for a model told to have no projector: " + joined
    )


def test_an_absent_key_STILL_auto_pairs_and_still_announces(tmp_path, monkeypatch):
    """The other polarity, and the arm that makes the pair a distinction.

    "none means text only" is also satisfied by a change that killed auto-pairing
    outright — which would silently revert T231 while every arm about `none`
    stayed green.
    """
    monkeypatch.setattr(llm_backend, "installed_flags", lambda: frozenset())
    monkeypatch.setattr(llm_backend, "installed_build", lambda: "test")
    model = _gguf(tmp_path / "v" / "Model-Q4.gguf")
    proj = _gguf(tmp_path / "v" / "mmproj-F16.gguf")
    dest = write_preset_ini(
        [ModelRow(key="model-q4", path=str(model), source="custom")],
        _Settings({"model-q4": {"ctx": 4096}}),        # a cfg WITHOUT mmproj
        dest=tmp_path / "out.ini",
    )
    assert f"mmproj = {proj}" in dest.read_text(encoding="utf-8")

    rows = [ModelRow(key="m", path=str(model), source="custom")]
    app, model_switch = _app_with(monkeypatch, rows, {"m": {"ctx": 4096}})
    model_switch._say_projector(app, "m")
    assert any("mmproj-F16.gguf" in s for s in app.said), app.said


def test_none_ROUND_TRIPS_through_the_settings_the_panel_writes(tmp_path, monkeypatch):
    """🔴 THE SENTINEL IS ONLY REAL IF IT SURVIVES A SAVE.

    `_collect_group` POPS an EMPTY field and stores a non-empty one verbatim, so
    "none" persists as a stored value while "" vanishes — which is precisely the
    distinction this feature is. Driven through the real `_collect_group` rather
    than by writing the dict by hand, because a hand-written dict would prove the
    reader and never the WRITER, and the writer is the half that can drop it.
    """
    import asyncio

    from litetui import app as m
    from litetui.plugins.model_switch import ModelConfigBody, ModelConfigScreen
    from textual.widgets import Input

    async def body():
        a = m.LiteTUI()
        a.available_models = ["m"]
        a.model_id = "m"
        a._connect = lambda: None
        a._fetch_ctx_window = lambda: None
        async with a.run_test(size=(120, 40)) as pilot:
            a.push_screen(ModelConfigScreen("m"))
            for _ in range(8):
                await pilot.pause()
            panel = a.screen.query_one(ModelConfigBody)
            field = panel.query_one("#ld-mmproj", Input)
            field.value = NO_PROJECTOR
            await pilot.pause()
            out = panel._collect_group("ld", [("mmproj", "", "text")], {})
            assert out.get("mmproj") == NO_PROJECTOR, out

            field.value = ""
            await pilot.pause()
            out = panel._collect_group("ld", [("mmproj", "", "text")], {"mmproj": "x"})
            assert "mmproj" not in out, (
                "clearing the field must REMOVE the key — that is what makes "
                f"absent and none different answers: {out}"
            )

    asyncio.run(body())
