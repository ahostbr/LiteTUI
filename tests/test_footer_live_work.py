"""T570 piece 1 — what is running without me shows in the footer.

Ryan (19:5x): "also sub agents and background process should show in the footer".

🔴 THE SPLIT IS ONE PREDICATE, NOT TWO. A task is a subagent or it is a
background process, and `split_live` is the only place that decides. Deriving it
twice is how a row eventually appears in both counts or in neither — and neither
mistake is visible in a number: `bg:3  agents:2` looks exactly as reasonable
when the truth is four tasks as when it is five.

⬜ ZERO RENDERS AS ABSENCE. An idle session is the common case; a permanent
`bg:0  agents:0` costs width to say nothing, and a chip APPEARING is itself the
signal that something started. That is asserted rather than left to taste,
because "show a zero" is the obvious thing for the next person to add back.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m  # noqa: E402
from litetui import tasks as tasks_mod  # noqa: E402


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    a.convo_id = "convo-1"
    return a


def task(tool: str, state: str = tasks_mod.RUNNING, started: float | None = None):
    t = tasks_mod.new_task(tool, {"command": f"{tool} something"}, "convo-1")
    t.state = state
    if started is not None:
        t.started = started
    return t


def add(a, *tasks):
    for t in tasks:
        a.bg_tasks[t.id] = t


# ── the split ──────────────────────────────────────────────────────────────

def test_a_task_is_a_subagent_or_a_background_process_never_both():
    subs, bg = tasks_mod.split_live([task("subagent"), task("bash"), task("powershell")])
    assert [t.tool for t in subs] == ["subagent"]
    assert sorted(t.tool for t in bg) == ["bash", "powershell"]
    # The two lists partition the input: no row lost, no row counted twice.
    assert len(subs) + len(bg) == 3


def test_only_running_tasks_count():
    # A finished task in the store is not work in flight, and the store keeps
    # finished rows: `load` marks everything that was running LOST at boot.
    subs, bg = tasks_mod.split_live([
        task("subagent"),
        task("subagent", state="done"),
        task("bash", state="lost"),
    ])
    assert len(subs) == 1
    assert bg == []


def test_newest_first():
    now = time.time()
    subs, _ = tasks_mod.split_live([
        task("subagent", started=now - 100),
        task("subagent", started=now - 1),
    ])
    assert subs[0].started > subs[1].started


# ── the chips ──────────────────────────────────────────────────────────────

def test_the_footer_counts_each_kind_separately():
    a = make_app()
    add(a, task("subagent"), task("subagent"), task("bash"))
    text = a.ctx_label_text.plain
    assert "bg:1" in text
    assert "agents:2" in text


def test_an_idle_session_shows_neither():
    a = make_app()
    assert "bg:" not in a.ctx_label_text.plain
    assert "agents:" not in a.ctx_label_text.plain

    # And a store full of FINISHED work is still idle.
    add(a, task("bash", state="done"), task("subagent", state="done"))
    assert "bg:" not in a.ctx_label_text.plain
    assert "agents:" not in a.ctx_label_text.plain


def test_each_chip_is_hideable_on_its_own():
    a = make_app()
    add(a, task("subagent"), task("bash"))

    a.settings.footer_show_bg = False
    assert "bg:" not in a.ctx_label_text.plain
    assert "agents:1" in a.ctx_label_text.plain

    a.settings.footer_show_bg = True
    a.settings.footer_show_subagents = False
    assert "bg:1" in a.ctx_label_text.plain
    assert "agents:" not in a.ctx_label_text.plain
