"""T081: the tool approval on the sidebar host — the one where a slip runs a tool.

🔴 WHY THIS DIALOG IS DIFFERENT FROM THE OTHER THREE. A ModalScreen traps focus
BY CONSTRUCTION. A SidePanel is a plain Widget mounted on the SAME screen as the
chat, so without a deliberate trap Tab walks out of a PENDING APPROVAL into the
message input — leaving a blocked turn behind a dialog nobody is looking at, one
unconsidered Enter from the wrong answer.

⚠️ NON-OBSCURING WAS THE FEATURE. NON-BLOCKING WAS NEVER ASKED FOR. Seeing the
chat while deciding is the improvement; tabbing into it is not.

📌 THE FOCUS ASSERTIONS NAME THE CONTROL, NOT THE CONTAINER. "focus is inside the
body" is satisfiable by the wrong button, and that exact weakness shipped a real
bug in T078 — focus drifted back to "Yes, stop" after a swap and the test I had
written could not see it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import tool_policy
from litetui.side_panel import DialogController, SidePanel, request_swap
from litetui.tool_approval import (
    ALWAYS,
    DENIED,
    ONCE,
    ToolApproval,
    ToolApprovalBody,
    ToolApprovalScreen,
)

BUTTONS = ["tool-approval-deny", "tool-approval-allow", "tool-approval-always"]


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _decision():
    return tool_policy.PolicyDecision(
        action=tool_policy.CONFIRM,
        profile="interactive",
        capabilities=frozenset({"PROCESS_EXECUTION"}),
        reason="test",
    )


def _body():
    return ToolApprovalBody("bash", {"command": "echo hi"}, _decision())


async def _open(a, pilot, style="sidebar"):
    """Open an approval and wait for it to SETTLE — not for a fixed tick count."""
    got: list = []
    ctrl = DialogController(a, _body, style, "right")

    async def _run():
        got.append(await ctrl.open())

    for _ in range(8):
        await pilot.pause()
    a.run_worker(_run(), name="approval")
    # 🔴 `focused is not None` IS VACUOUS IN A SIDEBAR and this used to rely on
    # it. The panel shares a screen with the chat, so the message input is
    # ALREADY focused before this dialog has laid out — the loop exited on the
    # first pause and everything after it raced. Wait for focus to be INSIDE THE
    # BODY, which is the thing that was actually meant.
    for _ in range(20):
        await pilot.pause()
        found = a.screen.query(ToolApprovalBody)
        if not found:
            continue
        focused = a.screen.focused
        if focused is not None and focused in found[0].walk_children(with_self=True):
            break
    return ctrl, got


async def _settle_clickable(pilot, app, selector, n=20):
    """Wait until `selector` is what the MOUSE would actually hit.

    Existing, queryable and laid-out are three different things. `get_widget_at`
    on the widget's own centre is the only check that asks the question a click
    asks — and asserting it on the first frame the body exists is a race that
    passed 1322/1322 on one machine and failed on another at the same sha.
    """
    for _ in range(n):
        await pilot.pause()
        hits = app.screen.query(selector)
        if not hits:
            continue
        w = hits[0]
        r = w.region
        if not r.width or not r.height:
            continue
        top = app.screen.get_widget_at(r.x + r.width // 2, r.y + r.height // 2)[0]
        if top is w or w in top.ancestors_with_self:
            return w
    return None


def test_denied_is_falsy_and_both_approvals_are_truthy() -> None:
    """SilverBolt's contract 1, re-asserted from this side of the conversion.

    The host returns None on cancel, which is falsy too — so `if not answer:` at
    the call site keeps failing closed across the port. This looks redundant and
    is the only thing standing between the codebase and a "tidy" refactor into
    strings, where `"deny"` is TRUTHY and Deny starts RUNNING the tool.
    """
    assert not DENIED
    assert ONCE and ALWAYS
    assert not ToolApproval(approved=False, remember=True)


@pytest.mark.asyncio
async def test_TAB_CANNOT_LEAVE_A_PENDING_APPROVAL() -> None:
    """🔴 THE FOCUS TRAP. Tab more times than there are controls and stay inside."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, got = await _open(a, pilot)
        body = a.screen.query_one(ToolApprovalBody)

        seen = []
        for _ in range(len(BUTTONS) * 2 + 2):
            await pilot.press("tab")
            await pilot.pause()
            fid = getattr(a.screen.focused, "id", None)
            seen.append(fid)
            assert fid in BUTTONS, (
                f"Tab escaped the approval onto {fid!r} — a pending tool call is "
                f"now behind a dialog nobody has focus in. Sequence: {seen}"
            )
        # It must CYCLE, not freeze on one control: a dialog you cannot move
        # around inside is its own failure.
        assert len(set(seen)) > 1, f"focus never moved: {seen}"
        assert got == [], "tabbing answered the dialog"
        ctrl.resolve(DENIED)


@pytest.mark.asyncio
async def test_shift_tab_also_stays_inside() -> None:
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, got = await _open(a, pilot)
        for _ in range(len(BUTTONS) + 2):
            await pilot.press("shift+tab")
            await pilot.pause()
            fid = getattr(a.screen.focused, "id", None)
            assert fid in BUTTONS, f"shift+tab escaped onto {fid!r}"
        ctrl.resolve(DENIED)


@pytest.mark.asyncio
async def test_a_swap_mid_decision_does_not_answer_and_does_not_run_the_tool() -> None:
    """The catastrophic path: a swap that resolves approves or denies a call
    nobody decided on."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, got = await _open(a, pilot)
        a.screen.query_one("#tool-approval-allow").focus()
        await pilot.pause()

        request_swap(a.screen.query_one(ToolApprovalBody))
        for _ in range(8):
            await pilot.pause()

        assert ctrl.style == "modal", "the swap did not change host"
        assert ctrl.pending, "THE SWAP RESOLVED THE APPROVAL"
        assert got == [], f"a swap delivered {got} — nobody decided that"
        # The user's own choice must still be under their finger.
        assert getattr(a.screen.focused, "id", None) == "tool-approval-allow", (
            f"the swap moved focus to {getattr(a.screen.focused, 'id', None)!r}; "
            "on this dialog that changes what the next Enter answers"
        )
        ctrl.resolve(DENIED)


@pytest.mark.asyncio
async def test_app_teardown_mid_decision_RELEASES_the_caller() -> None:
    """A swap must NOT resolve; teardown MUST. Those are opposites, and the
    caller is a blocked turn either way — one is a dangling answer, the other is
    a hang."""
    a = make_app()
    got: list = []
    ctrl = DialogController(a, _body, "sidebar", "right")

    async def _run():
        got.append(await ctrl.open())

    async with a.run_test(size=(120, 40)) as pilot:
        for _ in range(6):
            await pilot.pause()
        a.run_worker(_run(), name="approval")
        for _ in range(8):
            await pilot.pause()
        assert ctrl.pending, "the approval resolved before anyone answered"
        # Teardown, exactly as app exit does it.
        ctrl.resolve(None)
        for _ in range(4):
            await pilot.pause()

    assert got == [None], f"the caller was left blocked; got {got}"
    assert not got[0], "teardown must be FALSY — it is a denial, never an approval"


@pytest.mark.asyncio
async def test_the_modal_path_is_still_a_real_ToolApprovalScreen() -> None:
    """Identity: its own DEFAULT_CSS carries the dimming overlay and centering."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        result: list = []
        a.push_screen(ToolApprovalScreen("bash", {"c": "x"}, _decision()), result.append)
        await pilot.pause()
        assert isinstance(a.screen, ToolApprovalScreen)
        assert a.screen.query_one(ToolApprovalBody)
        await pilot.press("escape")
        await pilot.pause()
    assert result == [DENIED], "escape must deny"


@pytest.mark.asyncio
async def test_the_buttons_answer_a_real_MOUSE_CLICK_in_the_sidebar() -> None:
    """🔴 EVERY KEYBOARD PATH PASSED WHILE CLICKS WERE SWALLOWED.

    The first version of this body nested its box one level deeper than the
    original, and `get_widget_at` on the Deny button returned the BODY. Escape
    worked, `press()` worked, focus worked — only a real click failed. None of
    the tests written for the other two conversions click anything, so this is
    the assertion that would have caught it there too.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        ctrl, got = await _open(a, pilot)
        # Wait for the button to be REACHABLE BY THE MOUSE, not merely present.
        # Asserting hit-testing on the first available frame is what made this
        # test machine-dependent: green 1322/1322 here, red on another box at
        # the same sha.
        btn = await _settle_clickable(pilot, a, "#tool-approval-deny")
        assert btn is not None, (
            "the Deny button never became mouse-reachable — either nothing "
            "rendered or something is permanently on top of it"
        )
        await pilot.click("#tool-approval-deny")
        for _ in range(4):
            await pilot.pause()
    assert got == [DENIED], f"the click did not deny; got {got}"
