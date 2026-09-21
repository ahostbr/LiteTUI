"""T688 — two LiteTUI instances share one settings.json and must not erase
each other.

Ryan, 2026-09-12: *"one thing i wanna check is how two litetui processes coexist
with their settings, backend, model selection, think level etc ... make sure two
litetui instances coexist gracefully."* Two were live when this was written
(litetui.exe 39320 and 128748), both with cwd C:\\Projects\\LiteTUI, so both
resolved `paths.data_root()` to the same repo root and `settings_path()` to one
file.

🔴 THE LOSS IS NOT A RACE AND DOES NOT NEED ONE. `save()` wrote the WHOLE
in-memory dataclass, so the second instance to save did not have to interleave
with the first to undo it: it simply wrote back the snapshot it had loaded
minutes earlier. Seconds apart or hours apart, the later save wins every field,
including the seventy it never touched. That is why these arms save in sequence
with no threads — a timing test would have implied the bug needs bad luck.

⚠️ EVERY ARM USES A TEMP ROOT. Two instances were running against the real
C:\\Projects\\LiteTUI\\settings.json while this was written; a test that touched
the default root would have rewritten Ryan's live seat's settings.
"""

from __future__ import annotations

import json
from pathlib import Path

from litetui import settings as st


def _read(root: Path) -> dict:
    return json.loads(st.settings_path(root).read_text(encoding="utf-8"))


def test_a_second_instance_does_not_erase_the_first(tmp_path: Path) -> None:
    """The reproduction: A changes the think level, B later changes the theme."""
    # Both instances start from the same file, as two app launches would.
    st.save(st.Settings(), tmp_path)
    a = st.load(tmp_path)
    b = st.load(tmp_path)

    a.thinking_level = "xhigh"
    st.save(a, tmp_path)
    assert _read(tmp_path)["thinking_level"] == "xhigh"

    # B has been open this whole time and knows nothing about A's change.
    b.theme_name = "ash"
    st.save(b, tmp_path)

    on_disk = _read(tmp_path)
    assert on_disk["theme_name"] == "ash", "B's own change must survive"
    assert on_disk["thinking_level"] == "xhigh", (
        "B wrote back its stale snapshot and erased A's think level — the whole "
        "dataclass was saved, not the fields B actually changed"
    )


def test_the_field_a_second_instance_never_touched_is_left_alone(tmp_path: Path) -> None:
    """The general form, stated separately because it is the bigger blast radius.

    The arm above names two fields Ryan can see. This one says the rule: a save
    may only move what THIS instance changed. Seventy keys travel in that file,
    and a stale write moves all of them at once.
    """
    st.save(st.Settings(), tmp_path)
    stale = st.load(tmp_path)

    fresh = st.load(tmp_path)
    fresh.default_model = "qwen/qwen3.8-27b"
    fresh.max_tokens_chat = 8192
    st.save(fresh, tmp_path)

    stale.theme_name = "ash"
    st.save(stale, tmp_path)

    on_disk = _read(tmp_path)
    assert on_disk["default_model"] == "qwen/qwen3.8-27b"
    assert on_disk["max_tokens_chat"] == 8192


def test_env_override_does_not_overwrite_another_instances_default(tmp_path: Path) -> None:
    """An unrelated save preserves disk defaults and the instance's override."""
    import os

    assert os.environ.get("LITETUI_BACKEND"), "conftest no longer pins the backend"
    st.save(st.Settings(), tmp_path)
    a = st.load(tmp_path)
    b = st.load(tmp_path)

    # A writes a value straight into the file, behind B's back.
    raw = _read(tmp_path)
    raw["backend"] = "codex"
    st.settings_path(tmp_path).write_text(json.dumps(raw), encoding="utf-8")

    # B saves something unrelated; its environment stays effective-only.
    b.theme_name = "ash"
    st.save(b, tmp_path)
    assert _read(tmp_path)["backend"] == "codex"
    assert a.backend == b.backend == os.environ["LITETUI_BACKEND"]


def test_a_torn_file_cannot_be_observed(tmp_path: Path) -> None:
    """`write_text` truncates first, so a reader between the two syscalls sees an
    empty or half file — and `load()` answers that with SILENT DEFAULTS, because
    "one bad line must not stop the app" (settings.py:398). A truncated file is
    therefore not a crash but a settings reset nobody is told about.

    The property that makes it impossible is that the target is never opened for
    writing at all: the bytes go to a temp file and arrive by one atomic
    `os.replace`, the way tasks.py:290 and scheduler.py:402 already do it.
    """
    s = st.load(tmp_path)
    s.theme_name = "ash"
    st.save(s, tmp_path)

    p = st.settings_path(tmp_path)
    before = p.read_bytes()
    s.theme_name = "dawn"
    st.save(s, tmp_path)

    # No sibling temp file is left behind by a completed save.
    # ⚠️ THE PREFIX IS `.settings-`, WHICH IS WHAT `mkstemp` IS ACTUALLY GIVEN.
    # A first draft globbed "settings.json." — a name this code never produces,
    # so the assertion was true no matter what the save left behind. A cleanup
    # check that cannot name the litter is not a cleanup check.
    strays = [q.name for q in p.parent.iterdir() if q.name.startswith(".settings-")]
    assert strays == [], f"a temp file survived the save: {strays}"
    assert p.read_bytes() != before
    assert json.loads(p.read_text(encoding="utf-8"))["theme_name"] == "dawn"


def test_a_seat_does_not_persist_a_name_it_was_refused(tmp_path: Path) -> None:
    """The second instance registers as a GENERATED name and must not write the
    name it asked for back into the shared file.

    Ryan runs two seats from one repo; the second asked for `OpenBolt`, the
    registry refused the takeover because a live seat held it, and issued
    `OddFlag`. `Seat.name` is corrected from the register output
    (`harness._resolved_name`) but `settings.seat_name` is not — so the instance
    is called OddFlag and holds OpenBolt in memory.

    ⬜ NO NEW CODE ANSWERS THIS — the read-merge-write does. `seat_name` is
    unchanged since load, so a save of some other knob no longer carries it.
    It is asserted here rather than assumed, because "the fix for B also fixes
    C" is exactly the kind of claim that is pleasant and untested.
    """
    st.save(st.Settings(), tmp_path)
    other = st.load(tmp_path)
    other.seat_name = "OpenBolt"
    st.save(other, tmp_path)
    assert _read(tmp_path)["seat_name"] == "OpenBolt"

    # The second instance loads that file, is issued a different name by the
    # registry, and later changes something unrelated.
    second = st.load(tmp_path)
    assert second.seat_name == "OpenBolt", "it holds the name it asked for"
    second.theme_name = "ash"
    st.save(second, tmp_path)

    on_disk = _read(tmp_path)
    assert on_disk["theme_name"] == "ash"
    assert on_disk["seat_name"] == "OpenBolt", (
        "the file still records the name the USER chose; the second instance "
        "neither claimed it nor erased it"
    )


def test_a_model_switch_and_a_think_level_change_both_persist(tmp_path: Path) -> None:
    """The two knobs Ryan named, changed in different instances (T688 amendment).

    RUNTIME is already per instance — `settings_mod.load()` runs once per app
    (app.py:1140) and each holds its own `backend` and current model. What
    collided was PERSISTENCE: `model_switch.py:116` and `:828` both call
    `settings_mod.save(s)` after a model switch and after llama load settings,
    and `:906` writes the reasoning-effort override. Each of those wrote the
    whole dataclass, so whichever instance switched model last also published
    its stale think level.
    """
    st.save(st.Settings(), tmp_path)
    switcher = st.load(tmp_path)
    thinker = st.load(tmp_path)

    # A switches model and pins it, the way model_switch.py does.
    switcher.default_model = "qwen/qwen3.8-27b"
    switcher.pin_default_model = True
    st.save(switcher, tmp_path)

    # B, open all along, raises its think level.
    thinker.thinking_level = "xhigh"
    st.save(thinker, tmp_path)

    on_disk = _read(tmp_path)
    assert on_disk["thinking_level"] == "xhigh", "B's think level"
    assert on_disk["default_model"] == "qwen/qwen3.8-27b", "A's model survived B's save"
    assert on_disk["pin_default_model"] is True, "...and so did the pin beside it"

    # And the reverse order, because "last writer wins" is order-dependent and
    # one ordering passing proves nothing about the other.
    switcher2 = st.load(tmp_path)
    thinker2 = st.load(tmp_path)
    thinker2.thinking_level = "high"
    st.save(thinker2, tmp_path)
    switcher2.default_model = "gemma-3-4b-it.Q4_K_M"
    st.save(switcher2, tmp_path)

    on_disk = _read(tmp_path)
    assert on_disk["thinking_level"] == "high"
    assert on_disk["default_model"] == "gemma-3-4b-it.Q4_K_M"
