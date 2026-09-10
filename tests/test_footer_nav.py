"""T570 piece 2 — the footer takes the keyboard.

Ryan (19:5x): "allow down arrow to 'select' the footer then left right arrown to
nav the footer to switch between tool modes, think modes, background proccess
display and subagents dispaly".

🔴 THE SELECTION IS AN ID, NEVER AN INDEX, and that is the whole reason these
arms exist. The chip list CHANGES ON ITS OWN — `bg` and `agents` appear when work
starts and vanish when it finishes, with no key pressed. Under an index, chip 2
silently becomes a different chip mid-selection and the next Enter opens
something the user was not pointing at. Nothing about that looks wrong on screen:
the highlight is still on "a chip".
"""
from __future__ import annotations

import sys
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
    a._refresh_ctx_label = lambda: None
    a.bg_tasks = {}
    return a


def task(tool: str):
    return tasks_mod.new_task(tool, {"command": "x"}, "c1")


def add_tasks(a, *tools):
    for tool in tools:
        t = task(tool)
        a.bg_tasks[t.id] = t


# ── what is navigable ──────────────────────────────────────────────────────

def test_only_chips_that_are_on_screen_are_navigable():
    a = make_app()
    # Nothing running: bg and agents are not drawn, so they are not reachable.
    assert a.footer_nav_items() == ["authority", "think"]

    add_tasks(a, "bash")
    assert a.footer_nav_items() == ["authority", "think", "bg"]

    add_tasks(a, "subagent")
    assert a.footer_nav_items() == ["authority", "think", "bg", "agents"]


def test_a_hidden_chip_is_not_navigable():
    a = make_app()
    add_tasks(a, "bash", "subagent")
    a.settings.footer_show_thinking = False
    a.settings.footer_show_bg = False
    assert a.footer_nav_items() == ["authority", "agents"]


# ── moving ─────────────────────────────────────────────────────────────────

def test_down_takes_the_footer_at_the_first_chip():
    a = make_app()
    assert a._footer_nav is None
    a.footer_nav_enter()
    assert a._footer_nav == "authority"


def test_left_and_right_wrap():
    a = make_app()
    a.footer_nav_enter()
    a.footer_nav_move(1)
    assert a._footer_nav == "think"
    a.footer_nav_move(1)
    assert a._footer_nav == "authority", "right did not wrap"
    a.footer_nav_move(-1)
    assert a._footer_nav == "think", "left did not wrap"


def test_a_selected_chip_that_VANISHES_does_not_hand_the_next_key_to_a_stranger():
    """The case an index gets wrong, and the reason for the id.

    Select `bg`, then the task finishes — with no key pressed the chip is gone.
    The next Right must land somewhere real, and must NOT be "whatever is now at
    the index bg used to occupy".
    """
    a = make_app()
    add_tasks(a, "bash")
    a.footer_nav_enter()
    a.footer_nav_move(1)
    a.footer_nav_move(1)
    assert a._footer_nav == "bg"

    a.bg_tasks.clear()  # the process ended on its own
    assert "bg" not in a.footer_nav_items()

    a.footer_nav_move(1)
    assert a._footer_nav in a.footer_nav_items()
    assert a._footer_nav == "authority"


def test_leaving_gives_the_keyboard_back():
    a = make_app()
    a.footer_nav_enter()
    a.footer_nav_leave()
    assert a._footer_nav is None


# ── what it looks like ─────────────────────────────────────────────────────

def test_the_selected_chip_is_drawn_differently():
    a = make_app()
    plain_before = a.ctx_label_text.plain
    a.footer_nav_enter()
    styled = a.ctx_label_text
    # Same text, different styling: the highlight must not move anything.
    assert styled.plain == plain_before
    assert any("reverse" in str(sp.style) for sp in styled.spans), (
        "nothing is highlighted — the arrow key reads as having done nothing"
    )


def test_nothing_is_highlighted_when_the_footer_is_not_selected():
    a = make_app()
    assert not any("reverse" in str(sp.style) for sp in a.ctx_label_text.spans)


# ── activation calls the one body, never a copy ────────────────────────────

def test_enter_on_authority_cycles_through_the_existing_action(monkeypatch):
    a = make_app()
    called: list[bool] = []
    monkeypatch.setattr(type(a), "action_cycle_tool_profile", lambda self: called.append(True))
    a.footer_nav_enter()
    a.footer_nav_activate()
    assert called == [True]


def test_enter_on_think_opens_the_SAME_picker_slash_think_opens(monkeypatch):
    # Not a second picker body: `/think` with no argument is the one entry, and
    # it carries the no-screen-over-rpc guard that a copy here would lose.
    from litetui.plugins import misc as misc_mod

    seen: list[tuple] = []
    monkeypatch.setattr(misc_mod, "_cmd_think", lambda app, name, arg: seen.append((name, arg)))

    a = make_app()
    a.footer_nav_enter()
    a.footer_nav_move(1)
    assert a._footer_nav == "think"
    a.footer_nav_activate()
    assert seen == [("think", "")]
