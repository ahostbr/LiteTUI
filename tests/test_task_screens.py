"""T570 piece 3 — the two panels behind the `bg` and `agents` chips.

Ryan (19:5x): "both backgroudn proccess and subagents will need a new modal for
them. backgroudn proccess just need to show them running with time display etc
... sub agents should show prompt sent > thinking > response ... clearing when
the agent is done."

🔴 THE PILOT ARMS ARE THE POINT OF THIS FILE. Before piece 3, `footer_nav_activate`
fell THROUGH for `bg` and `agents`: the chips highlighted, Left/Right moved, and
Enter did nothing at all. Nothing about that is visible from a unit arm over
`footer_nav_activate` — it returns None either way, exactly as it did when it
was silently doing nothing. Only driving the real key and then asking the app
what is on its screen can tell those two apart.

🔴 AND A PANEL THAT NEVER CLEARS LOOKS EXACTLY LIKE ONE THAT IS STILL WORKING.
A finished agent left on screen with a frozen clock reads as a hang. `sync()`
rebuilding on a membership change is what "clearing when the agent is done"
actually is, so it gets an arm that finishes a task and re-syncs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _settle import settle_until  # noqa: E402
from textual.widgets import Static  # noqa: E402

from litetui import app as m  # noqa: E402
from litetui import task_screens as ts  # noqa: E402
from litetui import tasks as tasks_mod  # noqa: E402


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    return a


def task(tool: str, args: dict | None = None, started: float | None = None):
    t = tasks_mod.new_task(tool, args or {"command": f"{tool} something"}, "convo-1")
    if started is not None:
        t.started = started
    return t


def add(a, *tasks):
    for t in tasks:
        a.bg_tasks[t.id] = t
    return tasks[0] if tasks else None


# ── the prompt, which is why Task grew a field ─────────────────────────────

def test_the_label_cannot_answer_what_did_I_ask_it_and_the_prompt_can():
    """`label_of` is the first LINE cut to 60 chars. A subagent prompt is
    routinely neither one line nor short, so the panel asking `label` would show
    a truncated first sentence and call it the prompt."""
    args = {"prompt": "Audit the parser.\nReport every unchecked index.\nBe terse."}
    t = tasks_mod.new_task("subagent", args, "c1")
    assert "\n" not in t.label
    assert "unchecked index" not in t.label
    assert t.prompt == args["prompt"]


def test_a_huge_prompt_is_capped_and_says_so():
    t = tasks_mod.new_task("subagent", {"prompt": "x" * 9000}, "c1")
    assert len(t.prompt) == tasks_mod.PROMPT_CAP + 1
    assert t.prompt.endswith("…"), "a silent truncation reads as the whole prompt"


def test_the_prompt_survives_the_store(tmp_path):
    """The store is rewritten on every transition and re-read at boot. A field
    the panel depends on that does not round-trip is empty after a restart."""
    t = tasks_mod.new_task("subagent", {"prompt": "line one\nline two"}, "c1")
    tasks_mod.save([t], tmp_path)
    back = tasks_mod.load(tmp_path)[t.id]
    assert back.prompt == "line one\nline two"


def test_an_old_row_without_the_field_still_loads(tmp_path):
    """`load` filters to known fields, so a store written before this field
    existed must not raise — it must come back with an empty prompt."""
    import json

    row = tasks_mod.new_task("bash", {"command": "ls"}, "c1").to_row()
    row.pop("prompt")
    (tmp_path / tasks_mod.STORE).write_text(json.dumps([row]), encoding="utf-8")
    back = next(iter(tasks_mod.load(tmp_path).values()))
    assert back.prompt == ""


# ── one predicate, two panels ──────────────────────────────────────────────

def test_neither_panel_can_show_the_other_kind():
    a = make_app()
    sub = task("subagent", {"prompt": "count the leaves"})
    bg = task("bash", {"command": "sleep 30"})
    add(a, sub, bg)

    bg_out, agents_out = ts.bg_text(a), ts.agents_text(a)
    assert bg.id in bg_out and sub.id not in bg_out
    assert sub.id in agents_out and bg.id not in agents_out


def test_a_finished_task_is_in_neither():
    a = make_app()
    t = add(a, task("subagent", {"prompt": "done already"}))
    t.state = tasks_mod.DONE
    assert "no subagents running" in ts.agents_text(a)
    assert "no background processes" in ts.bg_text(a)


def test_the_background_row_carries_a_live_clock():
    a = make_app()
    add(a, task("bash", started=time.time() - 90))
    row = ts.bg_text(a)
    assert "1m 3" in row, f"no elapsed time in the row: {row!r}"
    assert tasks_mod.RUNNING in row


def test_the_agent_block_names_all_three_stages_and_shows_the_prompt():
    a = make_app()
    add(a, task("subagent", {"prompt": "Audit the parser.\nBe terse."}))
    block = ts.agents_text(a)
    for stage in ("prompt sent", "thinking", "response"):
        assert stage in block, f"stage {stage!r} missing from the panel"
    assert "Be terse." in block, "the panel showed a truncated label, not the prompt"


def test_the_thinking_stage_says_why_there_is_no_text():
    """The subagent call is `stream: False` — there is no partial output to
    show. A blank pane would read as the model being stuck, so the absence is
    stated. If subagent calls ever stream, this arm is the one that must change.
    """
    a = make_app()
    add(a, task("subagent", {"prompt": "think hard"}))
    assert "does not stream" in ts.agents_text(a)


# ── 🔴 no screen over rpc ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "door,tool", [(ts.open_background, "bash"), (ts.open_subagents, "subagent")]
)
def test_rpc_gets_the_data_and_never_a_dialog(door, tool, monkeypatch):
    """A dialog pushed over `--rpc` waits on a keyboard that is not attached,
    and the caller — a model — hangs holding a turn that can never finish
    (T558-A reproduced exactly that). The guard fails by HANGING, so it is
    asserted from both sides: the data went out, and `open_dialog` was NOT
    called."""
    opened: list = []
    monkeypatch.setattr(ts, "open_dialog", lambda *a, **k: opened.append(a))

    a = make_app()
    a._rpc = True
    t = add(a, task(tool, {"command": "sleep 30", "prompt": "count the leaves"}))
    said: list[str] = []
    a.system_message = said.append

    door(a)
    assert opened == [], "a screen was pushed over rpc — the caller would hang"
    assert said and t.id in said[0], "rpc got no data either"


def test_the_tui_path_still_opens_the_dialog(monkeypatch):
    """The negative control for the arm above: without `_rpc` the same door must
    reach `open_dialog`, or the guard would be indistinguishable from the whole
    feature being dead."""
    opened: list = []
    monkeypatch.setattr(ts, "open_dialog", lambda *a, **k: opened.append(a))
    a = make_app()
    add(a, task("subagent", {"prompt": "hello"}))
    ts.open_subagents(a)
    assert len(opened) == 1
    assert opened[0][1] is ts.SubagentsBody


# ── pilot: Enter on the chip reaches the stack ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chip,tool,body",
    [("bg", "bash", ts.BackgroundProcessesBody), ("agents", "subagent", ts.SubagentsBody)],
)
async def test_enter_on_the_chip_puts_the_panel_on_the_screen(chip, tool, body) -> None:
    a = make_app()
    async with a.run_test(size=(120, 34)) as pilot:
        a.settings.dialog_style = "modal"
        add(a, task(tool, {"command": "sleep 30", "prompt": "count the leaves"}))
        a._footer_nav = chip
        assert chip in a.footer_nav_items(), "the chip is not even navigable"

        a.footer_nav_activate()
        # 🔴 `a.screen.query`, NEVER `a.query`: App.query does not search the
        # screen STACK, so a dialog that mounted perfectly reads as nothing
        # having opened — an instrument that cannot reach the thing it is
        # asserting, reported as the feature being dead.
        assert await settle_until(pilot, lambda: bool(a.screen.query(body))), (
            f"Enter on the {chip} chip opened nothing — it falls through "
            "silently, which looks identical to a working key"
        )


@pytest.mark.asyncio
async def test_the_panel_clears_when_the_agent_is_done() -> None:
    a = make_app()
    async with a.run_test(size=(120, 34)) as pilot:
        a.settings.dialog_style = "modal"
        t = add(a, task("subagent", {"prompt": "count the leaves"}))
        a._footer_nav = "agents"
        a.footer_nav_activate()
        assert await settle_until(pilot, lambda: bool(a.screen.query(ts.SubagentsBody)))

        panel = a.screen.query_one(ts.SubagentsBody)
        assert t.id in panel.render_row(t)
        assert panel.rows(), "the agent was never in the panel to begin with"

        # It finished on its own — no key pressed, nothing closed.
        t.state = tasks_mod.DONE
        t.ended = time.time()
        assert panel.sync() is True, "the panel did not notice the agent finish"
        assert panel.rows() == []
        await pilot.pause()
        title = panel.query_one(".lt-title", Static)
        assert "Subagents (0)" in str(title.visual), (
            "the count still claims a live agent"
        )
        assert not panel.query(f"#lt-{t.id}"), "the finished agent is still drawn"


@pytest.mark.asyncio
async def test_the_clock_moves_without_rebuilding_the_panel() -> None:
    """A recompose every second would fight the scroll position and any focus in
    the panel, so a tick with the same membership must update text in place."""
    a = make_app()
    async with a.run_test(size=(120, 34)) as pilot:
        a.settings.dialog_style = "modal"
        t = add(a, task("bash", started=time.time() - 5))
        a._footer_nav = "bg"
        a.footer_nav_activate()
        assert await settle_until(
            pilot, lambda: bool(a.screen.query(ts.BackgroundProcessesBody))
        )

        panel = a.screen.query_one(ts.BackgroundProcessesBody)
        row = panel.query_one(f"#lt-{t.id}")
        t.started -= 60
        assert panel.sync() is False, "membership did not change; it rebuilt anyway"
        await pilot.pause()
        assert row is panel.query_one(f"#lt-{t.id}"), "the row widget was replaced"
        assert "1m" in str(row.visual), "the clock did not move"
