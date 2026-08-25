"""The authority level is VISIBLE and CYCLABLE. T084, Ryan's second ruling.

He gave the spec by example -- five screenshots of Claude Code's own footer,
one per shift+tab press ("see same way claude works ... shift+tab cycles how
permissions and how autonomous i let you b[e]"):

    >> auto mode on          ||  manual mode on          >> accept edits on

So: shift+tab advances the level and wraps; the footer names the level as a
plain phrase with a leading glyph that says at a glance whether this level
will interrupt you.

🔴 THE FOOTER FIELD HAS NO TOGGLE AND THAT IS THE POINT. T084 happened because
the authority actually in force was invisible while Settings showed something
else. A footer that can be switched off, or that only speaks up in the
restrictive cases, reproduces exactly that condition -- Claude paints
`>> auto mode on` as loudly as `>> bypass permissions on`.

⚠️ THE KEYBINDING HAS A HAZARD AT BOTH ENDS, AND I HIT THE FIRST ONE.
shift+tab is ALREADY bound inside dialogs (side_panel -> focus_prev_in_dialog,
ask_user_question -> prev_question), so the plan was to bind at app level
WITHOUT priority and let dialogs win by proximity.

MEASURED: that version never fires at all. Textual's own `Screen` binds
shift+tab to `focus_previous`, and a SCREEN binding beats an APP binding — so
the polite binding is dead in the chat too, not just in dialogs, and it fails
by doing NOTHING, which no test would have noticed unless one pressed the key.

With `priority=True` it fires everywhere, including over the dialog focus trap
— which would walk focus out of a pending tool approval. So precedence is
decided explicitly in `side_panel.handle_reverse_tab`, and the two gates are:
  test_shift_tab_cycles_the_level_at_app_level  -> it fires in the chat
  test_a_dialog_keeps_its_own_shift_tab         -> it does NOT fire in a dialog
Neither alone is sufficient; the first version passed the second and failed
the first.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui import tool_policy
from litetui.side_panel import DialogController, SidePanel
from litetui.textfmt import profile_text
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, SCHEDULED
from litetui.widgets import ConfirmStopBody


def make_app(profile=AUTONOMOUS):
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.settings.tool_policy_profile = profile
    a._active_tool_profile = profile
    return a


# ── the format: one source, derived glyph, absence as absence ──────────────

def test_the_phrase_reads_level_on_not_mode_level():
    """Ryan's screenshots read "auto mode on", never "mode: auto"."""
    assert profile_text(AUTONOMOUS).endswith(" on")
    assert AUTONOMOUS in profile_text(AUTONOMOUS)
    assert ":" not in profile_text(AUTONOMOUS)


def test_the_glyph_separates_WILL_INTERRUPT_from_WILL_NOT():
    """The glyph is the part you read without reading.

    autonomous runs everything; interactive stops to ask; scheduled stops by
    refusing. Asking and refusing are both interruptions from the user's side,
    which is why `scheduled` gets the stop glyph despite having an EMPTY
    confirm set -- the glyph answers "will this run?", not "does this prompt?".
    """
    assert profile_text(AUTONOMOUS).startswith(">>")
    assert profile_text(INTERACTIVE).startswith("||")
    assert profile_text(SCHEDULED).startswith("||")


def test_the_glyph_is_DERIVED_so_a_new_profile_cannot_be_forgotten(monkeypatch):
    """If this were a table of names, a fourth profile would render as the
    wrong glyph -- or crash -- and nothing would say so."""
    wide = tool_policy.ToolProfile(
        name="trusted", allow=tool_policy.CAPABILITIES,
        confirm=frozenset(), summary="new, grants everything",
    )
    narrow = tool_policy.ToolProfile(
        name="peek", allow=frozenset({tool_policy.READ_ONLY}),
        confirm=frozenset(), summary="new, grants almost nothing",
    )
    monkeypatch.setitem(tool_policy.PROFILES, "trusted", wide)
    monkeypatch.setitem(tool_policy.PROFILES, "peek", narrow)
    assert profile_text("trusted").startswith(">>")
    assert profile_text("peek").startswith("||")


@pytest.mark.parametrize("missing", [None, "", "a-profile-that-was-removed"])
def test_absence_renders_as_ABSENCE(missing):
    """No resolved profile yet, or a name no longer in PROFILES. Inventing a
    label for an authority we cannot name would be a claim, not a readout."""
    assert profile_text(missing) == ""


# ── the cycle: Ryan's order, wrapping ──────────────────────────────────────

def test_the_cycle_is_ryans_order_and_wraps():
    assert tool_policy.cycle(AUTONOMOUS) == INTERACTIVE
    assert tool_policy.cycle(INTERACTIVE) == SCHEDULED
    assert tool_policy.cycle(SCHEDULED) == AUTONOMOUS


def test_the_cycle_visits_every_profile_and_returns():
    """Derived over PROFILE_NAMES: a profile added later joins the cycle
    automatically instead of becoming unreachable from the keyboard."""
    seen, cur = [], AUTONOMOUS
    for _ in range(len(tool_policy.PROFILE_NAMES)):
        seen.append(cur)
        cur = tool_policy.cycle(cur)
    assert cur == AUTONOMOUS, "the cycle did not return to its start"
    assert sorted(seen) == sorted(tool_policy.PROFILE_NAMES), "a profile is unreachable"


# ── the footer: shows the RESOLVED level, and moves when it moves ──────────

@pytest.mark.asyncio
async def test_the_footer_names_the_level_and_follows_a_cycle():
    a = make_app(AUTONOMOUS)
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        assert profile_text(AUTONOMOUS) in a.ctx_label_text.plain

        a.action_cycle_tool_profile()
        await pilot.pause()
        assert a.settings.tool_policy_profile == INTERACTIVE
        assert profile_text(INTERACTIVE) in a.ctx_label_text.plain, (
            "the footer kept naming the old level after a cycle"
        )
        assert profile_text(AUTONOMOUS) not in a.ctx_label_text.plain


@pytest.mark.asyncio
async def test_the_footer_shows_the_RESOLVED_level_not_the_stored_one():
    """They differ per turn now, and the resolved one is what governs tools.

    Settings says `interactive`; an inbox-woken turn resolves to the read-only
    floor because nobody is there to answer a modal. The footer must say what
    is in force, which is the entire product requirement behind "show this in
    the footer".
    """
    a = make_app(INTERACTIVE)
    a._chat_running = lambda: False
    a._user_bubble = lambda *x, **k: None
    a._append = lambda *x, **k: None
    a._stream = lambda *x, **k: None
    async with a.run_test(size=(120, 45)) as pilot:
        a._deliver_inbox({"from": "abc", "priority": "normal", "body": "go"})
        await pilot.pause()
        assert a.settings.tool_policy_profile == INTERACTIVE
        assert profile_text(SCHEDULED) in a.ctx_label_text.plain, (
            "the footer showed the STORED level while a narrower one governed"
        )


# ── the keybinding, and the hazard it must not create ──────────────────────

@pytest.mark.asyncio
async def test_shift_tab_cycles_the_level_at_app_level():
    a = make_app(AUTONOMOUS)
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert a.settings.tool_policy_profile == INTERACTIVE


@pytest.mark.asyncio
async def test_a_dialog_keeps_its_own_shift_tab():
    """🔴 THE GATE ON THE FOCUS TRAP.

    The app binding IS priority, so nothing in Textual's resolution order
    stops it from beating SidePanel's focus_prev_in_dialog. The only thing
    that does is the explicit early return in action_cycle_tool_profile.
    Delete that return and this test goes red while everything else stays
    green -- including the "it fires in the chat" test next to it.

    Walking focus out of a PENDING TOOL APPROVAL is the concrete harm: the
    dialog stays open, the turn stays blocked, and the next Enter goes
    somewhere nobody is looking.
    """
    a = make_app(AUTONOMOUS)
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        ctrl = DialogController(a, ConfirmStopBody, "sidebar", "right")
        a.run_worker(ctrl.open(), name="dlg")
        opened = await settle_until(
            pilot, lambda: bool(a.screen.query(SidePanel))
        )
        assert opened, "the dialog never opened — this test proves nothing"

        before = a.settings.tool_policy_profile
        await pilot.press("shift+tab")
        await pilot.pause()
        assert a.settings.tool_policy_profile == before, (
            "shift+tab cycled the authority level while a dialog was open — "
            "the app binding stole the dialog's reverse-focus key"
        )


def test_the_binding_is_declared_WITH_priority():
    """Asserted on the declaration too, not only on the behaviour.

    ⚠️ THIS ASSERTED THE OPPOSITE FIRST, and the measurement changed it. The
    brief said to bind WITHOUT priority so dialogs win by proximity; that
    version never fired anywhere, because Textual's Screen already binds
    shift+tab. Precedence is now explicit in side_panel.handle_reverse_tab,
    and the behavioural test above is what actually protects dialogs.
    """
    binding = [b for b in m.LiteTUI.BINDINGS
               if getattr(b, "key", None) == "shift+tab"]
    assert len(binding) == 1, f"expected one shift+tab binding, got {len(binding)}"
    assert binding[0].priority is True, (
        "without priority the binding never fires: Textual's Screen already "
        "binds shift+tab to focus_previous and a screen beats an app"
    )


# ── persistence: the same ruling Ctrl+T got ────────────────────────────────

@pytest.mark.asyncio
async def test_cycling_PERSISTS_like_ctrl_t_does(monkeypatch):
    """Ryan on Ctrl+T this session: "make all three persist". A key that
    changes a setting the settings screen also shows must not leave the two
    disagreeing."""
    saved = []
    monkeypatch.setattr(m.settings_mod, "save", lambda s: saved.append(s.tool_policy_profile))
    a = make_app(AUTONOMOUS)
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        a.action_cycle_tool_profile()
        await pilot.pause()
    assert saved == [INTERACTIVE], f"the level was not persisted: {saved}"


@pytest.mark.asyncio
async def test_a_cycle_retargets_the_turn_in_flight():
    """`_execute_tool` reads `_active_tool_profile`, so a press that changed
    only the setting would leave the footer and the running tools disagreeing
    at exactly the moment someone is using the key to stop something."""
    a = make_app(AUTONOMOUS)
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        a.action_cycle_tool_profile()
        await pilot.pause()
    assert a._active_tool_profile == INTERACTIVE
