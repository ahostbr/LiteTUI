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
from litetui.llm_backend import ModelRow, sibling_mmproj, write_preset_ini


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
