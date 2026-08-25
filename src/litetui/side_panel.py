"""Sidebar/modal dialog hosting — ONE future, TWO disposable views, swappable live.

🔴 THE MECHANISM IS TEXTUAL'S OWN, NOT AN INVENTION. Textual 8.1.0's `HelpPanel`
(textual/widgets/_help_panel.py) is a plain `Widget` whose DEFAULT_CSS carries
`split: right`, and it arrives via `screen.mount(...)` rather than `push_screen`.
`split` carves space out of the layout, so the chat REFLOWS narrower instead of
being obscured. Three differences from a modal, total:

    Widget (not ModalScreen) · split: right · mount (not push_screen)

⚠️ OBSCURING AND BLOCKING ARE SEPARATE AXES AND ONLY ONE CHANGES. A decision
dialog still has to be answered before the turn proceeds; what changes is that
the chat stays READABLE while you decide. For the approval dialog that is a
safety improvement, not a relaxation. So this host is deliberately NOT
dismissable-by-clicking-away: the only exits are its buttons and Esc.

═══════════════════════════════════════════════════════════════════════════════
🔴 THE INVARIANT: A SWAP MUST NEVER RESOLVE THE FUTURE.
═══════════════════════════════════════════════════════════════════════════════
The caller has awaited and is still waiting. Tearing a host down to rebuild it
in the other style must not look like an answer. Get this wrong and a swap
silently ALLOWS or DENIES a tool call nobody decided on — the worst failure this
feature can produce, and a silent one.

That is why the future is owned by `DialogController` and NOT by either view:

    DialogController   owns the future, the style, and the carried state
    SidePanel          a disposable VIEW
    _ModalHost         a disposable VIEW
    only `resolve()`   ever completes the future — a real button or Esc

A view's teardown (`close_view`) is deliberately a DIFFERENT verb from
`resolve`. There is no code path where removing a view answers the dialog.

📌 AND THE MODAL VIEW USES `push_screen`, NOT `push_screen_wait`. `push_screen_wait`
would create a SECOND future that `ModalScreen.dismiss` resolves, and then two
futures would race to answer one dialog. One owner, one future.

📌 STATE IS CARRIED AS DATA, NOT AS A WIDGET. Textual's `remove()` prunes; it is
not a reliable re-parent. So a swap asks the body for `get_state()`, builds a
FRESH body in the new host, and calls `set_state()` on it. That is why
`show_dialog` takes a body FACTORY rather than a body instance.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widget import Widget

#: What a dialog body must offer to survive a swap. Bodies without these still
#: work — the state simply does not carry — so a body is never *required* to
#: implement them, but anything with input in it should.
BodyFactory = Callable[[], Widget]


class DialogController:
    """Owns the future. Views come and go beneath it."""

    def __init__(self, app, body_factory: BodyFactory, style: str,
                 side: str = "right", trap_focus: bool = True) -> None:
        self.app = app
        self.style = style
        #: Keep Tab inside the dialog while it is open. DEFAULT TRUE, because a
        #: ModalScreen has always trapped focus — so trapping is what PRESERVES
        #: existing behaviour across the conversion, and not trapping would be
        #: the silent change. Informational sidebars can opt out.
        self.trap_focus = trap_focus
        #: "right" | "left". Which edge a sidebar view docks to. Additive with a
        #: default, so existing three-arg construction is unaffected.
        self.side = side
        self._body_factory = body_factory
        self._future: asyncio.Future[Any] | None = None
        self._done = False
        self._result: Any = None
        self._view: Widget | ModalScreen | None = None
        self._state: dict = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def open(self) -> Any:
        self._future = asyncio.get_running_loop().create_future()
        await self._mount_view()
        if self._done:            # answered during mount; never await a done deal
            return self._result
        return await self._future

    async def _mount_view(self) -> None:
        body = self._body_factory()
        # 🔴 ASSIGN `_view` BEFORE THE AWAIT, NOT AFTER IT.
        #
        # `mount()` suspends, and the body is LIVE AND INTERACTIVE while it is
        # suspended — its own swap button included. A swap scheduled in that
        # window found `_view is None`, took the `if view is not None` branch in
        # `swap()` at face value, and skipped the `get_state()` capture
        # ENTIRELY. The dialog then rebuilt itself empty: everything the user
        # had typed, silently gone, with no error anywhere.
        #
        # Measured 5 in 30 runs at 8484923 (`assert '' == 'half-written
        # reason'`), and `get_state` was never called in any of the five.
        # `resolve()` has the same `_view is None` branch, so the same window
        # also leaked a view that was never torn down.
        #
        # There is no window to lose now: the controller owns the view from the
        # moment the view exists, which is before anyone can touch it.
        if self.style == "sidebar":
            view: Any = SidePanel(self, body)
            self._view = view
            await self.app.screen.mount(view)
        else:
            view = _ModalHost(self, body)
            self._view = view
            self.app.push_screen(view)

    async def swap(self) -> None:
        """Change style IN PLACE. Must not touch the future."""
        if self._done:
            return
        view, self._view = self._view, None
        if view is not None:
            body = view.body
            try:
                self._state = body.get_state()
            except (AttributeError, NoMatches):
                # AttributeError: a body without get_state simply carries nothing.
                # NoMatches: the body exists but has not composed its children
                # yet, so there is genuinely nothing typed to carry. Both are
                # "nothing to carry" — neither is "do not bother looking", which
                # is what the `_view is None` window used to do.
                self._state = {}
            await view.close_view()
        self.style = "modal" if self.style == "sidebar" else "sidebar"
        await self._mount_view()

    def apply_carried_state(self, body: Widget) -> None:
        """Called by a view once its body is mounted and has a size."""
        if not self._state:
            return
        try:
            body.set_state(self._state)
        except AttributeError:
            pass          # a body without set_state simply carries nothing
        except Exception:
            # A body whose set_state cannot find a control it expected. Losing
            # carried state is a visible, recoverable annoyance; letting it
            # propagate out of on_mount kills the dialog the user is waiting on,
            # which is not.
            pass

    def resolve(self, value: Any = None) -> None:
        """THE ONLY WAY THE DIALOG IS ANSWERED."""
        if self._done:
            return
        self._done = True
        self._result = value
        if self._future is not None and not self._future.done():
            self._future.set_result(value)
        view, self._view = self._view, None
        if view is not None:
            self.app.call_next(view.close_view)

    @property
    def pending(self) -> bool:
        """True while the caller is still waiting. The swap test asserts this."""
        return not self._done


class _ViewMixin:
    """Shared teardown + focus behaviour. Neither view may resolve the future."""

    controller: DialogController
    body: Widget
    #: Bounded re-defers while waiting for the body to compose. See _settle.
    _settle_tries: int = 4

    def _restore_focus_target(self):
        return getattr(self, "_prev_focus", None)

    def _settle(self) -> None:
        """Apply carried state and place focus — AFTER the body has composed.

        🔴 DOING THIS DIRECTLY IN `on_mount` IS TOO EARLY. A host's `on_mount`
        fires before its child's children exist, so a `set_state` that does
        `self.query_one(OptionList)` finds nothing. The demo body got away with
        it because `Input.value` is set on the widget the body yields directly;
        PickerBody reaches one level deeper, into the OptionList's own state, and
        that level is not there yet.

        The symptom is not an exception — it is a picker that quietly reopens on
        row 0 after a swap, having silently relocated the user's selection.

        ⚠️ AND IT CAN FIRE AFTER TEARDOWN. Deferring to the next refresh means a
        dialog answered immediately — a caller that resolves before the frame
        lands — has already removed this view by the time this runs. `self.screen`
        RAISES `NoScreen` rather than returning None in that state, so the guard
        has to be a real check, not a `getattr(..., None)`.
        """
        if not self.is_mounted:
            return
        # ⚠️ ONE REFRESH IS NOT ALWAYS ENOUGH. The two hosts compose on different
        # schedules — SidePanel is awaited into place, _ModalHost arrives through
        # push_screen — so "the body has composed" has to be OBSERVED rather than
        # assumed from a fixed number of frames. Wait for the body to have
        # children, bounded, instead of guessing a delay that works on this
        # machine.
        if self._settle_tries > 0 and not self.body.children:
            self._settle_tries -= 1
            self.call_after_refresh(self._settle)
            return
        self.controller.apply_carried_state(self.body)
        self._take_focus()

    def _focus_is_inside_body(self) -> bool:
        # `screen` is a property that RAISES when the node is detached — this
        # cannot be written as getattr(self, "screen", None).
        try:
            focused = self.screen.focused
        except Exception:
            return False
        if focused is None:
            return False
        return focused in self.body.walk_children(with_self=True)

    def _take_focus(self) -> None:
        """Focus the first focusable — UNLESS the carried state already placed it.

        🔴 THIS ORDERING WAS A REAL BUG, found by the first REAL dialog on this
        host. `on_mount` called `apply_carried_state` and then focused the first
        focusable unconditionally, so a body whose `set_state` restored focus had
        it CLOBBERED one line later. On ConfirmStop that put focus back on
        "Yes, stop" after a swap — while the user was deciding whether to stop.

        ⭐ The T075 focus test could not see it: it asserted focus landed
        *somewhere inside the body*, which is true of the wrong button too. An
        assertion satisfiable by the wrong answer is not a weaker test, it is an
        absent one.
        """
        if self._focus_is_inside_body():
            return
        for node in self.query("*"):
            if node.focusable:
                node.focus()
                return
        try:
            self.focus()
        except Exception:
            pass

    # Kept as the old name so nothing outside this module has to change.
    _focus_something = _take_focus


class SidePanel(Widget, _ViewMixin):
    """Right-hand split view. Carves space; the chat reflows narrower."""

    # 🔴 THE SIDE IS A CLASS, NOT A PATCHED STYLE. `split` decides the layout, so
    # it has to be right before the first layout pass; a class set in __init__ is,
    # and a style assigned in on_mount would relayout after one frame at the wrong
    # edge. `split: left` is first-class in Textual — verified against the shipped
    # lib, not assumed: textual.css.constants.VALID_EDGE == {bottom,left,none,right,top}.
    #
    # ⚠️ ONLY THE BORDER FLIPS. The padding here is `padding: 0 1` — SYMMETRIC — so
    # there is nothing directional to mirror. (HelpPanel carries `padding-right: 1`
    # and would need it; this panel never copied that, and mirroring a property the
    # artifact does not have is a change made against the source you copied FROM
    # rather than the thing you wrote.)
    DEFAULT_CSS = """
    SidePanel {
        width: 33%;
        min-width: 30;
        max-width: 60;
        height: 100%;
        padding: 0 1;
        layout: vertical;
    }
    SidePanel.-side-right {
        split: right;
        border-left: vkey $foreground 30%;
    }
    SidePanel.-side-left {
        split: left;
        border-right: vkey $foreground 30%;
    }
    """

    can_focus = True
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        # 🔴 THE FOCUS TRAP. A ModalScreen traps focus BY CONSTRUCTION; this
        # panel is a plain Widget mounted on the SAME screen as the chat, so
        # without these two bindings Tab walks straight out of a PENDING TOOL
        # APPROVAL and into the message input. The dialog stays open, the turn
        # stays blocked, and the next Enter goes somewhere nobody is looking.
        #
        # ⚠️ NON-OBSCURING WAS THE FEATURE. NON-BLOCKING WAS NEVER ASKED FOR.
        # Being able to SEE the chat while deciding is the improvement; being
        # able to TAB INTO IT and leave a decision dangling is not.
        Binding("tab", "focus_next_in_dialog", "Next", show=False),
        Binding("shift+tab", "focus_prev_in_dialog", "Previous", show=False),
    ]

    def __init__(self, controller: DialogController, body: Widget) -> None:
        # Signature deliberately UNCHANGED — the side rides on the controller.
        # A consumer (SilverBolt's tool-list dialog, T076) is building against
        # this constructor while this edit lands.
        side = "left" if getattr(controller, "side", "right") == "left" else "right"
        super().__init__(classes="-side-" + side)
        self.controller = controller
        self.body = body
        self._prev_focus = None

    def compose(self) -> ComposeResult:
        yield self.body

    def on_mount(self) -> None:
        self._prev_focus = self.screen.focused
        # Deferred: the body's own children do not exist yet. See _settle.
        self.call_after_refresh(self._settle)

    async def close_view(self) -> None:
        """Teardown ONLY. Deliberately not named dismiss, and never resolves."""
        prev = self._prev_focus
        await self.remove()
        if prev is not None:
            try:
                prev.focus()
            except Exception:
                pass

    def action_cancel(self) -> None:
        self.controller.resolve(None)

    # ── focus trap ───────────────────────────────────────────────────────────
    def _trap_targets(self) -> list[Widget]:
        return [n for n in self.body.walk_children(with_self=True) if n.focusable]

    def _cycle_focus(self, step: int) -> None:
        """Move focus WITHIN the body, wrapping at both ends.

        Deliberately a real cycle rather than "refuse to move": a dialog you
        cannot Tab around is its own accessibility failure, and the point is to
        keep focus inside the decision, not to freeze it on one control.
        """
        if not self.controller.trap_focus:
            self.screen.focus_next() if step > 0 else self.screen.focus_previous()
            return
        nodes = self._trap_targets()
        if not nodes:
            return
        try:
            cur = self.screen.focused
        except Exception:
            return
        i = nodes.index(cur) if cur in nodes else (-1 if step > 0 else 0)
        nodes[(i + step) % len(nodes)].focus()

    def action_focus_next_in_dialog(self) -> None:
        self._cycle_focus(1)

    def action_focus_prev_in_dialog(self) -> None:
        self._cycle_focus(-1)


REVERSE_TAB = "shift+tab"


def _own_binding(node, key: str):
    """The action `node` itself binds to `key`, or None.

    Reads the node's OWN merged binding map rather than a hand-kept list of
    dialog classes -- which is the bug this function was rewritten to fix.
    """
    bindings = getattr(node, "_bindings", None)
    if bindings is None:
        return None
    found = getattr(bindings, "key_to_bindings", {}).get(key)
    if not found:
        return None
    return found[0].action


def handle_reverse_tab(app) -> bool:
    """shift+tab arrived at the APP. Does something nearer own it? Then run it.

    🔴 THIS EXISTS BECAUSE THE OBVIOUS ARRANGEMENT DOES NOT WORK, AND THE
    FAILURE IS SILENT IN BOTH DIRECTIONS.

    T084 binds shift+tab at app level to cycle the authority profile, the way
    Claude Code's shift+tab cycles its permission modes. The plan was to
    declare it WITHOUT `priority`, so a focused dialog's own shift+tab would
    win by proximity. Measured instead: Textual's own `Screen` already binds
    shift+tab to `focus_previous`, and a SCREEN binding beats an APP binding —
    so the non-priority version never fired ANYWHERE, dialog or not.

    With `priority=True` it fires everywhere, including over bindings that
    mean something else entirely. So proximity is reimplemented HERE, because
    the resolution order gives no way to say "app, except where something
    nearer already means something by this key".

    🔴 AND THE FIRST VERSION OF THIS FUNCTION ENUMERATED DIALOG CLASSES, WHICH
    IS WHY IT SHIPPED A REGRESSION. It knew about `SidePanel` and `_ModalHost`
    and nothing else, so `AskUserQuestionBody` -- which binds shift+tab to
    `prev_question`, NOT to reverse focus -- lost its key and
    test_ask_user_question.py went from 48/48 to 43/48. A list of the
    surfaces I happened to remember is the same drift pair this whole task
    exists to remove.

    So: walk from the focused node upward and run the FIRST node that binds
    this key itself, skipping `Screen` (whose generic `focus_previous` is
    exactly the binding this feature replaces). That is Textual's own
    proximity rule, applied by hand because priority took it away.
    """
    try:
        screen = app.screen
    except Exception:
        return False                      # no screen yet: nothing to own it

    focused = getattr(app, "focused", None)
    if focused is not None:
        for node in focused.ancestors_with_self:
            if node is app or isinstance(node, Screen):
                break
            action = _own_binding(node, REVERSE_TAB)
            if action is None:
                continue
            method = getattr(node, f"action_{action}", None)
            if method is None:
                continue
            method()
            return True

    # BACKSTOP: a dialog is open but focus is not inside it (the focus trap
    # should prevent this; if it ever fails, cycling the authority level from
    # behind an open approval is the wrong answer).
    if isinstance(screen, _ModalHost) or screen.query(SidePanel):
        screen.focus_previous()
        return True
    return False


class _ModalHost(ModalScreen, _ViewMixin):
    """Modal view. Same body, same controller, same future."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, controller: DialogController, body: Widget) -> None:
        super().__init__()
        self.controller = controller
        self.body = body

    def compose(self) -> ComposeResult:
        yield self.body

    def on_mount(self) -> None:
        # Deferred for the same reason as SidePanel's. See _settle.
        self.call_after_refresh(self._settle)

    async def close_view(self) -> None:
        """Pop WITHOUT dismissing: dismiss would answer a push_screen_wait."""
        if self.app.screen is self:
            self.app.pop_screen()

    def action_cancel(self) -> None:
        self.controller.resolve(None)


def _controller_for(widget: Widget) -> DialogController | None:
    for node in widget.ancestors_with_self:
        ctrl = getattr(node, "controller", None)
        if isinstance(ctrl, DialogController):
            return ctrl
    return None


def close_dialog(widget: Widget, value: Any = None) -> None:
    """Answer the dialog containing `widget`. The body's only exit.

    🔴 THE FALLBACK IS LOAD-BEARING, NOT DEFENSIVE. A body must also work inside
    a PLAIN `ModalScreen` that this module never created — which is exactly how
    the existing dialogs stay convertible without breaking their identity. The
    codebase asserts `isinstance(screen, ConfirmStop)` and binds CSS by class
    name (`ConfirmStop, PickerScreen, HelpScreen, SettingsScreen { ... }`), so a
    conversion that replaced those screens with `_ModalHost` would change what
    the modal path IS while claiming to only add a sidebar.

    Without this branch a body hosted by its own ModalScreen calls close_dialog
    and NOTHING HAPPENS — no error, no dismissal, a dead button.
    """
    ctrl = _controller_for(widget)
    if ctrl is not None:
        ctrl.resolve(value)
        return
    screen = widget.screen
    if screen is not None:
        screen.dismiss(value)


def request_swap(widget: Widget) -> None:
    """Swap the host under `widget` without answering the dialog."""
    ctrl = _controller_for(widget)
    if ctrl is not None:
        ctrl.app.call_next(ctrl.swap)


def present_dialog(app, body_factory: BodyFactory, modal_factory, callback=None) -> None:
    """Sidebar when the setting says so; otherwise THE ORIGINAL MODAL SCREEN.

    ⚠️ THE MODAL BRANCH DELIBERATELY DOES NOT GO THROUGH THIS MODULE. It calls
    `push_screen(modal_factory(), callback)` — the exact line the call site used
    before — so with the setting at its default, converted dialogs are not merely
    equivalent to the old behaviour, they ARE the old behaviour: same class, same
    CSS selector, same `isinstance` identity, same screen stack.

    Routing the modal branch through `_ModalHost` would have been tidier and
    would have silently changed what `ConfirmStop` and `PickerScreen` ARE. Two
    tests bind to that identity (`test_modals`, `test_modal_centering`) and would
    have caught it — but only after the claim "this only adds a sidebar" had
    already been written down.
    """
    if getattr(app.settings, "dialog_style", "modal") == "sidebar":
        open_dialog(app, body_factory, callback,
                    style="sidebar", side=app.settings.dialog_side)
    else:
        app.push_screen(modal_factory(), callback)


def open_dialog(app, body_factory: BodyFactory, callback=None, *,
                style: str | None = None, side: str | None = None,
                trap_focus: bool = True) -> None:
    """Open a dialog and hand the answer to `callback`. The `push_screen` shape.

    🔴 THIS EXISTS BECAUSE `show_dialog` FIT ONE CONSUMER IN FOUR. The T075 spike
    shipped an await-only surface, validated against a demo body whose caller I
    also wrote — so the host was tested against my own assumptions rather than
    against the codebase. Read from the real call sites afterwards:

        ToolApprovalScreen   await push_screen_wait(...)         AWAIT     fits
        ConfirmStop          push_screen(screen, callback)        CALLBACK  did not
        PickerScreen x5      push_screen(screen, callback)        CALLBACK  did not
        AskUserQuestion      pushed from a WORKER THREAD          neither

    `push_screen(screen, callback)` is this codebase's idiom, so the host has to
    speak it. With this, a conversion is a one-line swap at the call site instead
    of a rewrite of the caller into a worker — and a conversion that forces its
    callers to restructure is one that quietly does not get done.

    Deliberately returns None rather than the Worker: a caller that can `await`
    should be using `show_dialog`, and handing back an awaitable here would make
    two ways to do the same thing look interchangeable when they are not.
    """
    async def _run() -> None:
        result = await show_dialog(app, body_factory, style=style, side=side,
                                   trap_focus=trap_focus)
        if callback is not None:
            callback(result)

    app.run_worker(_run(), name="dialog")


async def show_dialog(app, body_factory: BodyFactory, *, style: str | None = None,
                      side: str | None = None, trap_focus: bool = True,
                      modal_factory=None) -> Any:
    """Show a dialog and await its answer, honouring `dialog_style`.

    `body_factory` is called once per host — a swap builds a fresh body and
    replays `get_state()` into it. Returns whatever was passed to
    `close_dialog`, or None if cancelled: the same contract `push_screen_wait`
    already has, so an awaiting caller does not change when the style does.
    """
    if style is None:
        style = app.settings.dialog_style
    if side is None:
        side = app.settings.dialog_side
    if style != "sidebar" and modal_factory is not None:
        # THE ORIGINAL SCREEN, awaited exactly as before. Same reason as
        # present_dialog's modal branch: a real dialog's own DEFAULT_CSS is
        # SCOPED TO ITS CLASS, so routing it through `_ModalHost` renders it
        # unstyled and changes what the modal IS. No controller here, so no
        # swap — which matches: the modal branch never had one.
        return await app.push_screen_wait(modal_factory())
    return await DialogController(app, body_factory, style, side, trap_focus).open()
