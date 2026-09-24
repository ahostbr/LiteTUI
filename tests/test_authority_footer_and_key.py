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
from litetui import textfmt
from litetui.textfmt import profile_text
from litetui.tool_policy import AUTONOMOUS, INTERACTIVE, STRICT
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

    autonomous runs everything; interactive and strict stop to ask. (The
    refusing `scheduled` level that took the stop glyph with an EMPTY confirm
    set is gone -- Ryan 2026-09-24: "remove scheduled completely it makes no sense to me ... make
    interactive ask only for dangerous cmds any deletions or zip expansions weird
    procc runs that arent its tools and dangerous cmds threw PS and bash"; the derivation test below
    still pins a refusing profile, because the glyph answers "will this
    run?", not "does this prompt?".)
    """
    assert profile_text(AUTONOMOUS).startswith(">>")
    assert profile_text(INTERACTIVE).startswith("||")
    assert profile_text(STRICT).startswith("||")


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
    """T085 took `scheduled` off the cycle ("scheduled should not be its own
    mode"); 2026-09-24 removed it outright. Three levels, stepping down."""
    assert tool_policy.cycle(AUTONOMOUS) == INTERACTIVE
    assert tool_policy.cycle(INTERACTIVE) == "strict"
    assert tool_policy.cycle("strict") == AUTONOMOUS


def test_scheduled_is_GONE_and_UNREACHABLE_from_the_keyboard():
    """It was the floor `unattended()` degraded to; both are removed --
    Ryan 2026-09-24: "remove scheduled completely it makes no sense to me ... make
    interactive ask only for dangerous cmds any deletions or zip expansions weird
    procc runs that arent its tools and dangerous cmds threw PS and bash".
    Pressing shift+tab must never land on it."""
    assert "scheduled" not in tool_policy.PROFILES
    reached = {tool_policy.cycle(n) for n in tool_policy.PROFILE_NAMES}
    assert "scheduled" not in reached, "the removed level is reachable by cycling again"


def test_cycling_OFF_scheduled_lands_back_in_the_selectable_set():
    """An old settings.json can still hold it. One press must escape, not
    stick — and must land on the NARROWEST selectable level, never the widest."""
    assert tool_policy.cycle("scheduled") == STRICT


def test_the_selectable_set_is_every_profile():
    """Was `..._DERIVED_from_the_profile_flag`: `ToolProfile.selectable`
    existed only to hide `scheduled`, and went with it (2026-09-24). The
    cycle and the dropdown still read one source, now all of PROFILES."""
    assert tool_policy.selectable_profile_names() == tool_policy.PROFILE_NAMES


def test_an_unrecognised_profile_cycles_DOWN_not_up():
    """Corrupt settings plus one keypress must not reach full authority.

    (It used to match `unattended()`, removed 2026-09-24.) The
    first version of `cycle` returned AUTONOMOUS here and no test noticed --
    found by re-reading my own diff, not by a red.
    """
    # T085: the landing moved from `scheduled` to `interactive` — not a
    # loosening, but the consequence of the floor leaving the selectable set.
    # What is asserted is unchanged: unknown lands on the NARROWEST level a
    # human may hold, never the widest.
    landed = tool_policy.cycle("not-a-profile")
    assert landed == tool_policy.selectable_profile_names()[0] == STRICT
    assert landed != AUTONOMOUS, "corrupt settings plus one keypress reached max authority"


def test_the_cycle_visits_every_SELECTABLE_profile_and_returns():
    """Derived over the selectable set: a profile added later joins the cycle
    automatically, and one marked unselectable leaves it automatically."""
    order = tool_policy.selectable_profile_names()
    seen, cur = [], AUTONOMOUS
    for _ in range(len(order)):
        seen.append(cur)
        cur = tool_policy.cycle(cur)
    assert cur == AUTONOMOUS, "the cycle did not return to its start"
    assert sorted(seen) == sorted(order), "a selectable profile is unreachable"


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
async def test_the_footer_shows_the_RESOLVED_level_not_the_stored_one(monkeypatch):
    """They differ per turn, and the resolved one is what governs tools.

    Settings says `interactive`; a cron fire resolves to autonomous because
    nobody is there to answer a modal (T085). The footer must say what is in
    force, which is the entire product requirement behind "show this in the
    footer".

    📌 This drove an INBOX turn, which resolved to the `scheduled` floor. That
    floor is gone (Ryan 2026-09-24: "remove scheduled completely it makes no sense to me ... make
    interactive ask only for dangerous cmds any deletions or zip expansions weird
    procc runs that arent its tools and dangerous cmds threw PS and bash") -- mail now KEEPS
    the stored level -- so the case where stored and resolved still differ is
    the cron fire.
    """
    from litetui import scheduler
    monkeypatch.setattr(m.sched_mod, "save", lambda *_a, **_k: None)
    a = make_app(INTERACTIVE)
    a._chat_running = lambda: False
    a._user_bubble = lambda *x, **k: None
    a._append = lambda *x, **k: None
    a._stream = lambda *x, **k: None
    async with a.run_test(size=(120, 45)) as pilot:
        job = scheduler.Job(prompt="nightly", schedule="@daily")
        a.jobs[:] = [job]
        a._fire_job(job)
        await pilot.pause()
        assert a.settings.tool_policy_profile == INTERACTIVE
        assert a._active_tool_profile == AUTONOMOUS
        assert profile_text(AUTONOMOUS) in a.ctx_label_text.plain, (
            "the footer showed the STORED level while another one governed"
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


@pytest.mark.asyncio
async def test_ASK_USER_QUESTION_keeps_shift_tab_for_prev_question():
    """🔴 THE SCOPE I MISSED, AND IT SHIPPED A REGRESSION FOR ONE COMMIT.

    `AskUserQuestionBody` binds shift+tab to `prev_question` -- NOT to reverse
    focus. The first `handle_reverse_tab` enumerated dialog CLASSES, knew about
    SidePanel and _ModalHost, and silently ate this one:
    test_ask_user_question.py went 48/48 -> 43/48 at cc230c8.

    My dialog test used ConfirmStopBody in a SidePanel and passed, which is
    exactly a control that validates the INSTRUMENT and not the SCOPE -- one
    dialog surface proved, the other assumed. This test is the second surface,
    and the fix is a proximity walk so a THIRD surface needs no new entry here.
    """
    from litetui.ask_user_question import AskUserQuestionBody, QuestionState

    a = make_app(AUTONOMOUS)
    states = [
        QuestionState(label="one", question="q1?",
                      options=[{"title": "a", "description": ""}]),
        QuestionState(label="two", question="q2?",
                      options=[{"title": "b", "description": ""}]),
    ]
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        body = AskUserQuestionBody(states, __import__("threading").Event(), [])
        await a.screen.mount(body)
        await settle_until(pilot, lambda: bool(a.screen.query(AskUserQuestionBody)))
        body.focus()
        await pilot.pause()

        before = a.settings.tool_policy_profile
        body.action_next_question()
        await pilot.pause()
        assert body._active == 1, "premise: next_question moved to q2"

        await pilot.press("shift+tab")
        await pilot.pause()

        assert a.settings.tool_policy_profile == before, (
            "shift+tab cycled the authority level inside AskUserQuestion — the "
            "app binding ate prev_question"
        )
        assert body._active == 0, (
            "shift+tab did not move back a question — prev_question never ran"
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


# -- T085: the light warning, and the migration that stops a crash --------

def test_the_creation_note_states_the_REASON_not_just_the_rule():
    """Ryan asked for a "light warning when setting that it must run auto for
    this reason". The reason IS the request.

    A note that only says "scheduled tasks run in auto mode" is a fact the
    reader can do nothing with. This asserts the mechanism is present -- that
    nobody may be there, and that asking would therefore wait -- because that
    is what lets someone predict a case nobody wrote down.
    """
    note = textfmt.SCHEDULED_AUTO_NOTE.lower()
    assert "auto" in note
    assert "keyboard" in note, "the note does not say WHY (nobody is there)"
    assert "wait" in note or "hang" in note, (
        "the note does not say what would go wrong instead"
    )
    # LIGHT: one sentence, no shouting, not a confirmation prompt.
    assert note.count(".") == 0, "more than one sentence"
    assert textfmt.SCHEDULED_AUTO_NOTE == textfmt.SCHEDULED_AUTO_NOTE.lstrip(), "padded"


def test_BOTH_creation_paths_show_the_note(monkeypatch, tmp_path):
    """/loop and /cron are two surfaces onto one decision. One text, used by
    both, so they cannot drift into saying different things."""
    from litetui import goal_loop, scheduler, paths as paths_mod

    said = []
    app = make_app(AUTONOMOUS)
    app.jobs.clear()
    app._system = lambda m, *a, **k: said.append(str(m))
    app._materialise_convo = lambda: None
    app.convo_id = "c1"
    monkeypatch.setattr(goal_loop.scheduler, "save", lambda jobs, root=None: None)

    goal_loop.loop_command(app, "15m check the deploy")
    assert said, "/loop said nothing"
    assert textfmt.SCHEDULED_AUTO_NOTE in said[-1], (
        f"/loop did not show the note: {said[-1]!r}"
    )

    # /cron goes through CronService, which owns its own confirmation line.
    from litetui import cron as cron_mod
    said.clear()
    svc = cron_mod.CronService(app)
    monkeypatch.setattr(cron_mod.sched_mod, "save", lambda jobs, root=None: None)
    svc.add("@daily summarise yesterday")
    assert said, "/cron said nothing"
    assert textfmt.SCHEDULED_AUTO_NOTE in said[-1], (
        f"/cron did not show the note: {said[-1]!r}"
    )


def test_a_stored_scheduled_level_does_not_CRASH_the_settings_screen(tmp_path):
    """🔴 MEASURED BEFORE IT WAS FIXED, NOT IMAGINED.

    `scheduled` was an offered choice until T085 removed it, so a settings.json
    written by yesterday's build holds it. The screen builds its dropdown as
    `Select(choices, value=stored, allow_blank=False)`, and textual raises
    InvalidSelectValueError from `Select._on_mount` when the stored value is
    not among the options -- verified directly by mounting one.

    That is the same class as a settings FIELD with no control (which breaks
    Save from every tab) arriving from the other side: a stored VALUE with no
    option. Both take out a screen the user opened to fix something else.
    """
    import json
    from litetui import settings as settings_mod

    (tmp_path / "settings.json").write_text(
        json.dumps({"tool_policy_profile": "scheduled"}), encoding="utf-8"
    )
    loaded = settings_mod.load(tmp_path)
    assert loaded.tool_policy_profile in tool_policy.selectable_profile_names()
    # 2026-09-24: `scheduled` MIGRATES to interactive (which now asks only
    # before dangerous actions) -- a deliberate landing, not the narrowest.
    assert loaded.tool_policy_profile == INTERACTIVE, (
        "a stored scheduled did not migrate to interactive"
    )


def test_the_migration_never_lands_on_AUTONOMOUS(tmp_path):
    """Never the widest. Someone who chose read-only must not be silently
    upgraded to "never asks" -- `scheduled` lands on interactive (2026-09-24),
    anything unrecognised on strict, the narrowest."""
    import json
    from litetui import settings as settings_mod

    for stored in ("scheduled", "not-a-real-profile", ""):
        (tmp_path / "settings.json").write_text(
            json.dumps({"tool_policy_profile": stored}), encoding="utf-8"
        )
        got = settings_mod.load(tmp_path).tool_policy_profile
        assert got != AUTONOMOUS, f"{stored!r} was widened to autonomous"
        assert got == (INTERACTIVE if stored == "scheduled" else STRICT), (stored, got)


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
