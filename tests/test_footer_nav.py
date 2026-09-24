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

import pytest  # noqa: E402
from _settle import settle_until  # noqa: E402

from litetui import app as m  # noqa: E402
from litetui import tasks as tasks_mod  # noqa: E402
from litetui.picker import PickerScreen  # noqa: E402


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._refresh_ctx_label = lambda: None
    a.bg_tasks = {}
    a.convo_id = "c1"
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
    assert a.footer_nav_items() == ["authority", "plan", "think"]

    add_tasks(a, "bash")
    assert a.footer_nav_items() == ["authority", "plan", "think", "bg"]

    add_tasks(a, "subagent")
    assert a.footer_nav_items() == ["authority", "plan", "think", "bg", "agents"]


def test_a_hidden_chip_is_not_navigable():
    a = make_app()
    add_tasks(a, "bash", "subagent")
    a.settings.footer_show_thinking = False
    a.settings.footer_show_bg = False
    assert a.footer_nav_items() == ["authority", "plan", "agents"]


def test_navigation_follows_the_custom_left_to_right_order():
    a = make_app()
    add_tasks(a, "bash", "subagent")
    a.settings.footer_order = ["agents", "think", "authority", "bg", "plan"]
    assert a.footer_nav_items() == ["agents", "think", "authority", "bg", "plan"]


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
    assert a._footer_nav == "plan"      # T573: plan sits between authority and think
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
    for _ in range(3):                  # authority -> plan -> think -> bg (T573)
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
    monkeypatch.setattr(type(a), "action_cycle_tool_profile",
                        lambda self, source="shift+tab": called.append(source))
    a.footer_nav_enter()
    a.footer_nav_activate()
    assert called == ["footer chip"]


# ── typing leaves the footer (Ryan 2026-09-24: "it switched on its own") ────

@pytest.mark.asyncio
async def test_typing_after_down_hands_enter_back_to_the_draft(monkeypatch) -> None:
    """Down selected the authority chip; typed text went into the draft while
    the chip stayed selected, so Enter cycled the authority (interactive ->
    strict, persisted) instead of sending, and the Enter after "nothing
    happened" landed on autonomous. Typing must leave the footer."""
    a = make_app()
    sent: list[str] = []
    monkeypatch.setattr(type(a), "_submit_text", lambda self, text, *args, **kw: sent.append(text))
    async with a.run_test(size=(120, 34)) as pilot:
        a.settings.tool_policy_profile = a._active_tool_profile = "interactive"
        await pilot.press("down")
        assert a._footer_nav == "authority"
        await pilot.press("h", "i")
        assert a._footer_nav is None, "typing kept the footer selected"
        await pilot.press("enter")
        assert await settle_until(pilot, lambda: sent == ["hi"]), f"Enter did not send the draft: {sent!r}"
    assert a.settings.tool_policy_profile == "interactive"


def test_enter_on_the_chip_still_cycles_and_says_where_it_came_from(monkeypatch):
    from litetui import runtime_log

    a = make_app()
    said: list[str] = []
    recorded: list[dict] = []
    monkeypatch.setattr(type(a), "_system", lambda self, text, *args, **kw: said.append(text))
    monkeypatch.setattr(runtime_log, "record", lambda event, **meta: recorded.append({"event": event, **meta}))
    a.settings.tool_policy_profile = a._active_tool_profile = "interactive"
    a.footer_nav_enter()
    a.footer_nav_activate()
    changed = a.settings.tool_policy_profile
    assert changed != "interactive"
    assert recorded == [{"event": "authority_change", "previous": "interactive", "profile": changed,
                         "source": "footer chip"}]
    assert any(f"interactive → {changed} (footer chip)" in line for line in said), said


def test_enter_on_think_runs_THE_SAME_command_typing_it_runs(monkeypatch):
    """Not a second picker body: `/think` with no argument is the one entry, and
    it carries the no-screen-over-rpc guard that a copy here would lose.

    🔴 THROUGH THE REGISTRY, NOT AN IMPORT. This arm used to monkeypatch
    `plugins.misc._cmd_think` and assert the call — which passed while app.py
    was importing a plugin submodule, the one thing
    test_plugin_dogfood.py::test_app_never_imports_a_plugin_module forbids. It
    was red at 6b25437 and nothing in the footer neighbourhood could see it. The
    dispatch door is asserted instead, plus that the name actually RESOLVES:
    a chip that sends `/thinkk` would print "Unknown" and look like a dead key.
    """
    a = make_app()
    seen: list[str] = []
    monkeypatch.setattr(type(a), "_handle_command", lambda self, cmd: seen.append(cmd))

    a.footer_nav_enter()
    a.footer_nav_move(1)
    a.footer_nav_move(1)                # authority -> plan -> think (T573)
    assert a._footer_nav == "think"
    a.footer_nav_activate()
    assert seen == ["/think"]
    assert "/think" in a.plugins.commands, (
        "the chip sends a name the registry does not know — it would print Unknown"
    )


@pytest.mark.asyncio
async def test_enter_on_think_really_reaches_the_picker() -> None:
    """The end-to-end half of the arm above.

    Asserting `_handle_command("/think")` proves the DISPATCH and not the
    outcome: a registry that lost the entry, a handler that raised, or a picker
    that never mounted would all still satisfy it, and every one of them looks
    like the arrow key having done nothing. This drives the real key path and
    then asks the app what is on its screen.
    """
    a = make_app()
    async with a.run_test(size=(120, 34)) as pilot:
        a.settings.dialog_style = "modal"
        a.footer_nav_enter()
        a.footer_nav_move(1)
        a.footer_nav_move(1)            # authority -> plan -> think (T573)
        assert a._footer_nav == "think"
        a.footer_nav_activate()
        # `a.screen.query`, never `a.query`: App.query does not search the
        # screen STACK, so a picker that mounted perfectly reads as nothing
        # having opened.
        assert await settle_until(
            pilot, lambda: isinstance(a.screen, PickerScreen) or bool(a.screen.query(PickerScreen))
        ), "Enter on the think chip opened no picker"


# ── plan (T573 piece 2) ────────────────────────────────────────────────────

def test_plan_is_navigable_whether_the_mode_is_on_or_off():
    """A chip reachable only while the mode is ON is a switch with no OFF
    position — you could leave plan mode from the footer and never enter it
    there. Ryan asked for a toggle, so it is drawn in both states."""
    a = make_app()
    assert "plan" in a.footer_nav_items()
    a._plan_mode = True
    assert "plan" in a.footer_nav_items()


def test_the_plan_chip_says_which_way_it_is_set():
    a = make_app()
    assert "plan:off" in a.ctx_label_text.plain
    a._plan_mode = True
    assert "plan:on" in a.ctx_label_text.plain


def test_enter_on_the_plan_chip_toggles_the_mode():
    """Through `action_toggle_plan_mode`, the same body Ctrl+P runs — the
    conversation rebuild lives in `set_plan_mode` and a second copy here is the
    one that would forget it."""
    a = make_app()
    a._system = lambda *args, **kwargs: None
    a.footer_nav_enter()
    a._footer_nav = "plan"
    before = a._plan_mode
    a.footer_nav_activate()
    assert a._plan_mode is not before, "Enter on the plan chip did not toggle"
    a.footer_nav_activate()
    assert a._plan_mode is before, "a second Enter did not toggle back"


def test_the_navigable_list_is_the_order_the_footer_draws():
    """The list and the renderer must agree, or Left/Right walks a sequence the
    user cannot see. Asserted by reading the drawn label, not by restating the
    list — a second copy of the order is the thing that drifts."""
    a = make_app()
    drawn = a.ctx_label_text.plain
    positions = []
    for chip in a.footer_nav_items():
        needle = {"authority": None, "plan": "plan:", "think": "think:"}.get(chip)
        if needle is None:
            continue
        assert needle in drawn, f"{chip} is navigable but not drawn"
        positions.append(drawn.index(needle))
    assert positions == sorted(positions), (
        f"navigable order {a.footer_nav_items()} does not match the drawn order "
        f"in {drawn!r}"
    )
