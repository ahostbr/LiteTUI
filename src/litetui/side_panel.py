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

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import Button

#: What a dialog body must offer to survive a swap. Bodies without these still
#: work — the state simply does not carry — so a body is never *required* to
#: implement them, but anything with input in it should.
BodyFactory = Callable[[], Widget]


class _SwapRequested:
    """A THIRD OUTCOME FOR A DIALOG THAT HAD TWO: "change host", never "answer".

    🔴 WHY THE OBVIOUS FIX IS THE WRONG ONE. `present_dialog`'s modal branch
    pushes the ORIGINAL `ModalScreen`, which carries no `DialogController` — so
    the swap control had nothing to ask, and on that branch it was a NO-OP for
    the whole life of this feature. Giving that screen a controller would fix it
    and would also change what the modal IS: same-class `isinstance`, the CSS
    keyed by class name, and the two tests that bind to both. That branch's
    docstring exists to defend exactly that.

    So the screen keeps its identity and its EXIT becomes tri-state. It is
    dismissed with this value, and the ROUTER that pushed it — the only code that
    still holds the body factory and the caller's callback — re-opens the same
    dialog on the sidebar host. The caller is never handed this value and is
    never told a swap happened, because it is still waiting.

    ⚠️ FALSY ON PURPOSE, AND NOTHING RELIES ON IT. `_execute_tool` reads
    `if not answer:` and `_on_stop_answer` reads `if not stop:`; both routers
    intercept the sentinel before either can see it, and there is one arm per
    consumer proving it. The falsiness is what happens IF an interception is ever
    removed: deny the tool and keep the turn, rather than approve something
    nobody decided on.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:          # pragma: no cover — debugging aid
        return "<SWAP>"


SWAP = _SwapRequested()


def _mark_swap_host(screen):
    """Tag a screen whose dismissal a router is watching for `SWAP`.

    Without the tag, `request_swap` cannot tell a screen a ROUTER pushed from one
    a CALLER pushed itself — and dismissing the second kind with the sentinel
    would hand a value nobody expects to somebody else's `push_screen` callback.
    """
    screen._litetui_swap_host = True
    return screen


def _is_swap_host(screen) -> bool:
    return bool(getattr(screen, "_litetui_swap_host", False))


def _screen_of(widget: Widget):
    """`Widget.screen` RAISES when the node is detached — it never returns None.

    A helper because `getattr(widget, "screen", None)` LOOKS like the guard and
    is not one: the property raises rather than being absent, so the getattr
    default never fires and the exception escapes. `_ViewMixin` carries the same
    note one class down, having learned it the same way.
    """
    try:
        return widget.screen
    except Exception:
        return None


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
        # Capture the host worker ONCE, here, where open() runs INSIDE the
        # worker awaiting the dialog. A later swap re-runs _mount_view from an
        # app callback (get_current_worker() would be None there), so every
        # rebuilt body must reuse THIS ref, not re-resolve its own.
        try:
            from textual.worker import get_current_worker
            self._host_worker = get_current_worker()
        except Exception:
            self._host_worker = None
        await self._mount_view()
        if self._done:            # answered during mount; never await a done deal
            return self._result
        return await self._future

    async def _mount_view(self) -> None:
        body = self._body_factory()
        # The host worker was captured ONCE in open() (inside the awaiting
        # worker); reuse that exact ref for every rebuilt body — a swap re-runs
        # this from an app callback where get_current_worker() is None. The
        # controller back-ref + current-body pointer let a body tell whether it
        # is still the ACTIVE view (an owned-dialog activity gate excludes its
        # own host worker; a completion must not re-render a superseded body).
        body._dialog_host_worker = getattr(self, "_host_worker", None)
        body._dialog_controller = self
        self._body = body
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
            # 🔴 AWAITED, LIKE ITS SIBLING ABOVE — AND THAT ASYMMETRY WAS T252.
            # Un-awaited, `_mount_view` returned while `_ModalHost` had not
            # entered `_compose`. A second swap 2-4 ticks later pruned that
            # half-composed subtree: `App._prune` marks `_pruning` across
            # `walk_children` (app.py:4302), `Widget.mount` then EARLY RETURNS
            # SILENTLY on `_pruning` (widget.py:1424-1425) so a `Select` never
            # got its children, and `_pre_process` dispatched `events.Mount()`
            # unconditionally anyway (message_pump.py:591) -> `Select._on_mount`
            # -> `query_one(SelectOverlay)` -> NoMatches -> the app died.
            #
            # ⚠️ THIS MAKES THE ASSIGNMENT ORDER ABOVE MORE LOAD-BEARING, NOT
            # LESS: the body is live and interactive for LONGER while this
            # suspends, so `self._view = view` must stay before the await for
            # exactly the reason the comment at the top of this method gives.
            self._view = view
            await self.app.push_screen(view)

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



def _is_composing(root: Widget) -> bool:
    """True while a descendant is ATTACHED but has not composed its children yet.

    The predicate is `children`, and it is the one that was MEASURED to flip.
    `is_mounted` looks like the right question and is not: `_is_mounted` is set
    in `_pre_process`'s `finally` (message_pump.py:612), and a `Select` mounted
    into a live body read `is_mounted is False` for six `asyncio.sleep(0)` ticks
    AFTER its own children already existed. A guard built on it would exhaust
    its tries every single time and never fire, which is a guard that defends
    the bug rather than catching it.

    The class test is what keeps this honest for widgets that have no children
    by design. A `Label` never composes, so asking "has it composed?" of one
    would be permanently False; only a class that overrides `compose` is asked.
    """
    return any(
        type(w).compose is not Widget.compose and not w.children
        for w in root.walk_children(Widget, with_self=True)
    )


async def await_subtree_composed(root: Widget) -> None:
    """Bounded wait before a teardown prunes `root`. THE T704 GUARD.

    IT LIVES ON `close_view`, NOT ON THE SWAP THAT REPORTED THE BUG. T704 was
    found through `DialogController.swap`, but `swap` is only one of the two
    callers — `resolve()` does `call_next(view.close_view)` for every dialog
    that is ANSWERED, which is the common case. Guarding the swap call site
    would have fixed the path the card names and left the other one exactly as
    broken. Both hosts call it because both define their own teardown.

    WHAT GOES WRONG WITHOUT IT, read out of the installed Textual 8.1.0 source
    rather than inferred. `App._prune` marks `_pruning` across `walk_children`;
    `Widget.mount` then EARLY RETURNS `AwaitMount(self, [])` on `_pruning`
    (widget.py:1424) — so a `Select` that is attached but has not yet run
    `_pre_process` composes, hands its `SelectOverlay` to
    `mount_composed_widgets`, and that mount is silently dropped.
    `_pre_process` dispatches `Compose()` and then `Mount()` unconditionally
    anyway (message_pump.py:599/604) and sets `_is_mounted` in its `finally`
    regardless — so `Select._on_mount` (_select.py:623) runs on a `Select` with
    no children, calls `_setup_options_renderables`, and that does
    `self.query_one(SelectOverlay)` at _select.py:546 -> NoMatches, raised out
    of a handler, which kills the app.

    CORRECTION TO THE COMMENT IN `_mount_view` ABOVE: the order is Compose THEN
    Mount, not "a Select never got its children so Mount crashed". Compose does
    run. It is the mount OF WHAT COMPOSE PRODUCED that `_pruning` throws away.

    THIS NARROWS THE WINDOW, IT DOES NOT CLOSE IT — and the bound is why. An
    unbounded wait inside a teardown is a hung dialog, so this reuses
    `_ViewMixin._settle_tries` and then proceeds exactly as the code did
    before. Under a load heavy enough that four ticks are not enough, the race
    is still reachable. The unit arm proves the GUARD; only the seven-file
    reproducer measures the product's own timing.
    """
    for _ in range(_ViewMixin._settle_tries):
        if not _is_composing(root):
            return
        await asyncio.sleep(0)


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

    /* 🔴 THE BODY MUST FILL THE PANEL, AND ONLY THE PANEL CAN SAY SO.
     *
     * Every dialog body was written for a ModalScreen, where `height: auto` and
     * a `max-height: 85%` are right: the box is as tall as its content and the
     * screen centres it. Mount that same body in a full-height panel and it
     * renders as a FLOATING BOX in the top corner — which is what Ryan saw.
     *
     * Fixing this on each body would need one edit per body plus a promise to
     * remember for the next one. It belongs HERE, on the host, because "fill the
     * panel" is the PANEL's requirement and is true of every body ever mounted
     * in one, including bodies that do not exist yet.
     *
     * max-width/max-height are overridden too: those caps exist to stop a MODAL
     * covering the whole screen, and a strip that already carves its own space
     * has no such problem.
     *
     * The `.-side-*` prefix is deliberate, not decoration: a bare `SidePanel >
     * *` ties on specificity with a body's own `PickerBody {...}` rule and the
     * winner would depend on source order. Type+class beats type, so this wins
     * predictably.
     */
    SidePanel.-side-right > *, SidePanel.-side-left > * {
        width: 100%;
        height: 100%;
        max-width: 100%;
        max-height: 100%;
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
        await await_subtree_composed(self.body)   # T704
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

    # ⚠️ BACKSTOP -- AND IT IS STILL A HAND-KEPT CLASS LIST, DEMOTED RATHER
    # THAN REMOVED. Reached only when the walk above found nothing, i.e. when a
    # dialog is open but focus is NOT inside it. SidePanel's focus trap exists
    # to make that impossible, so this is a net under a net -- which is exactly
    # what makes it easy to leave wrong: when it fires, something else has
    # already failed and nobody is looking here.
    #
    # 🔴 IF IT IS EVER WRONG IT FAILS PERMISSIVE: a dialog surface named by
    # neither class falls through and the authority level cycles from behind an
    # open approval. Kept as a list deliberately -- a marker attribute only
    # relocates "remember to add it", and registering open dialogs on the app
    # is a change to the dialog LIFECYCLE, which is not worth making on a path
    # with no demonstrated defect.
    #
    # 📌 THE CHECK THAT WOULD CATCH A NEW SURFACE, and the one I failed to run
    # against my own fix: `grep -rn '"shift+tab"' src/litetui/`. I ran it to
    # FIND the hazard and never re-ran it to VERIFY my coverage of it -- the
    # enumeration was available the whole time. Two real surfaces exist; my
    # first list named one of them plus `_ModalHost`, which does not bind this
    # key at all. One true entry, one irrelevant entry, one miss, reading as
    # deliberate coverage.
    if isinstance(screen, _ModalHost) or screen.query(SidePanel):
        screen.focus_previous()
        return True
    return False


class _ModalHost(ModalScreen, _ViewMixin):
    """Modal view. Same body, same controller, same future."""

    # 🔴 THIS HOST HAD NO CSS AT ALL, SO IT CENTRED NOTHING.
    #
    # The app stylesheet centres modals BY CLASS NAME — `ConfirmStop,
    # PickerScreen, HelpScreen, ...` — which is why the ORIGINAL screens look
    # right. `_ModalHost` is a class that stylesheet has never heard of, so a
    # dialog SWAPPED from the sidebar to modal landed against the top-left while
    # the very same body opened directly as a modal looked fine.
    #
    # Nothing caught it because the swap tests assert the future is still
    # pending and the state carried — both true of a dialog rendered in the
    # corner. Geometry needed its own gate: tests/test_dialog_geometry.py.
    DEFAULT_CSS = """
    _ModalHost {
        align: center middle;
    }
    """

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
        await await_subtree_composed(self.body)   # T704
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


class SwapButton(Button):
    """THE swap control. One widget, used by every dialog body.

    🔴 ONE CONTROL, NOT FOUR COPIES. T075 built this behaviour in
    `dialog_demo.py` and the conversion briefs for the four real dialogs did not
    carry it, so `grep -rn demo-swap` returned the demo and nothing else. Copying
    the button into four bodies would have meant four copies of the relabel rule
    as well -- four places to disagree about what the label should say. The
    relabel logic below is MOVED from the demo, not duplicated.

    📌 IT NAMES THE DESTINATION, NOT THE CURRENT STATE. "Sidebar" on a sidebar
    dialog reads equally as a state and as an action; "Open as modal" can only be
    read as the action. That reasoning is the demo's and it survives the move.

    ⚠️ THE SWAP MUST NEVER RESOLVE THE DIALOG. It changes the HOST; the
    controller keeps owning the future. `request_swap` is the only exit used
    here precisely because `close_dialog` is the one that answers.
    """

    #: 🔴 THE CONTROL CARRIES ITS OWN STYLE, AND THAT IS THE WHOLE POINT.
    #: Textual scopes DEFAULT_CSS to the DECLARING class — the mechanism that
    #: broke the sidebar conversion, because the four dialog bodies' rules were
    #: declared on the ModalScreens they were lifted out of and applied nowhere
    #: once they moved. Declared HERE, the rule is scoped to SwapButton, so it
    #: follows the button into any host: sidebar, modal, or a dialog nobody has
    #: written yet.
    #:
    #: ⇒ The same scoping that was the bug is the fix, used the right way round.
    #: It also keeps this control out of the four bodies' stylesheets entirely,
    #: so adding the button needs ONE line per body and no CSS edit at all.
    #: `.inline` is for a body that puts the control in a Horizontal beside its
    #: other buttons — the three list panels. Without it the `width: 100%` above
    #: takes the whole row and squeezes "Close" to nothing: the rule that is
    #: right for a control on its own line is wrong for one in a row, and the
    #: control owns both for the same reason it owns the first.
    DEFAULT_CSS = """
    SwapButton { width: 100%; margin: 0 0 1 0; }
    SwapButton.inline { width: auto; margin: 0 1 0 0; }
    """

    DEFAULT_ID = "dialog-swap"

    def __init__(self, id: str | None = None, *, classes: str | None = None) -> None:
        # `classes` is passed through for `.inline` — a body that puts the
        # control in a Horizontal beside its other buttons. Without the
        # passthrough the class is silently unavailable and the only way to get
        # the narrow variant is a private button again, which is the thing this
        # class exists to stop.
        super().__init__("", id=id or self.DEFAULT_ID, classes=classes)

    def on_mount(self) -> None:
        self.relabel()

    @on(Button.Pressed)
    def _swap(self, event: Button.Pressed) -> None:
        """🔴 THE CONTROL OWNS ITS OWN PRESS — AND IT DID NOT, UNTIL T222.

        The press was wired by each BODY instead: a private
        `@on(Button.Pressed, "#<its-own-id>") -> request_swap(self)` in
        dialog_demo, tool_list, loop_list and mcp_list — and in NONE of the four
        real dialogs (ConfirmStop, Picker, ToolApproval, AskUserQuestion), which
        yield this control and wire nothing. Pressing it there did nothing at
        all, in EITHER host, including the sidebar where the controller exists.

            git log --oneline -S"request_swap" -- src/litetui/widgets.py \\
                src/litetui/picker.py src/litetui/tool_approval.py \\
                src/litetui/ask_user_question.py          -> EMPTY
            git log --oneline -S"Button.Pressed" -- src/litetui/side_panel.py
                                                      -> EMPTY

        Never wired in those four, at any sha — not unwired by a later commit.

        A handler each body must copy is one chance per body to be the body that
        forgot, which is the argument the DEFAULT_CSS note above already makes,
        lost the same way. Owning the press makes the count one.

        ⚠️ `event.stop()` IS NOT TIDINESS. Bodies handle `Button.Pressed` to
        ANSWER the dialog. A swap that kept bubbling could be read by one of them
        as an answer — silently allowing or denying a tool call nobody decided
        on, which is the single failure this whole module is built to prevent.
        """
        event.stop()
        request_swap(self)

    def relabel(self) -> None:
        """Label for where pressing it TAKES you — and HIDE it where it cannot go.

        📌 THIS IS `swappable()`'s CALLER, AND IT HAD NONE. That method's own
        docstring says "callers use this to decide whether to offer the control
        at all, rather than offering it and hoping", and
        `grep -rn "swappable()" src/litetui` returned only its definition. A
        promise documented and not kept reads, to the next author, exactly like
        one that is kept.

        Both routers mark their screens now, so the hidden case is narrow and
        real: a body inside a plain `ModalScreen` that no router pushed —
        `ask_user_question`'s `loop is None` fallback, or a future bare
        `push_screen` — where the control genuinely cannot act.
        """
        in_sidebar = any(isinstance(n, SidePanel) for n in self.ancestors_with_self)
        self.label = "Open as modal" if in_sidebar else "Dock to side"
        self.display = self.swappable()

    def swappable(self) -> bool:
        """Would pressing this actually do anything HERE?

        🔴 THE HONEST ANSWER IS NOT ALWAYS YES, AND THAT IS THE WHOLE POINT OF
        THIS METHOD. `request_swap` needs a `DialogController`, and only
        `SidePanel` and `_ModalHost` ever carry one. `present_dialog`'s modal
        branch deliberately pushes the ORIGINAL `ModalScreen` -- so a body shown
        that way has NO controller and a swap there is a no-op.

        A button that is visible, pressable and inert is the defect class this
        whole day has been spent clearing. `relabel()` calls this to decide
        whether to offer the control at all, rather than offering it and hoping.

        ⇒ AND THE ANSWER CHANGED IN T222. A screen a ROUTER pushed can now swap
        WITHOUT a controller, by being dismissed with `SWAP` — see
        `_SwapRequested`. So "no controller" no longer means "inert"; only "no
        controller AND no router watching this screen's exit" does.
        """
        if _controller_for(self) is not None:
            return True
        screen = _screen_of(self)
        return screen is not None and _is_swap_host(screen)


def request_swap(widget: Widget) -> None:
    """Swap the host under `widget` without answering the dialog.

    Two hosts, two mechanisms, one invariant: NEITHER answers the dialog. With a
    controller the view is rebuilt underneath the future. Without one — a screen
    a router pushed — the screen's DISMISSAL is the only channel back to the code
    that still knows the body factory and the caller's callback, so the swap
    travels as `SWAP` and that router re-opens on the other host.

    A screen nobody routed is left alone: dismissing it with a sentinel would
    hand a value its own caller never asked for.
    """
    ctrl = _controller_for(widget)
    if ctrl is not None:
        ctrl.app.call_next(ctrl.swap)
        return
    screen = _screen_of(widget)
    if screen is not None and _is_swap_host(screen):
        screen.dismiss(SWAP)


def _dialog_name(factory, fallback: str = "This dialog") -> str:
    """A name for a body/modal factory, including a `functools.partial` of one."""
    fn = getattr(factory, "func", factory)
    return getattr(fn, "__name__", None) or fallback


def refuse_over_rpc(app, what: str) -> bool:
    """True when this session is headless and the dialog must NOT be opened.

    🔴 THE GUARD LIVES IN THE DOORS, NOT IN THE COMMANDS (T572). T558-A measured
    the failure on `/think` and fixed that one command; the mechanism was never
    specific to it. A screen pushed over `--rpc` waits on a keyboard that is not
    attached, and the caller — a model, not a person — hangs holding a turn that
    can never finish. Twelve commands reach a dialog through `present_dialog` or
    `open_dialog`; a per-command list is wrong the day someone adds the
    thirteenth, and wrong SILENTLY, because the symptom is a hang in another
    process.

    ⚠️ IT REFUSES, IT DOES NOT ANSWER. The callback is not called with None:
    `present_dialog`'s contract already distinguishes "Esc, answered with None"
    from "swapped, did not answer", and a fabricated None here would be read by
    a caller as the human having cancelled.

    ⚠️ AND IT SAYS SO OUT LOUD. Silence is indistinguishable from a command that
    did nothing, which is the state this replaces.
    """
    if not getattr(app, "_rpc", False):
        return False
    app.system_message(
        f"{what} is a keyboard dialog, and this session is headless (--rpc). It "
        f"was NOT opened: a screen here waits on a keyboard that is not attached "
        f"and the turn could never finish. Most commands take the same choice as "
        f"an argument instead — `/think high`, `/model <id>`, `/loop list`. "
        f"`/help` lists them."
    )
    return True


def present_dialog(app, body_factory: BodyFactory, modal_factory, callback=None,
                   what: str = "") -> None:
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
    # BOTH BRANCHES, because only the modal half pushes from here — the sidebar
    # half delegates to `open_dialog`, which carries the same guard. Checked
    # before the fork so the answer cannot depend on a setting nobody sets for a
    # headless child.
    if refuse_over_rpc(app, what or _dialog_name(modal_factory)):
        return
    if getattr(app.settings, "dialog_style", "modal") == "sidebar":
        open_dialog(app, body_factory, callback,
                    style="sidebar", side=app.settings.dialog_side)
        return

    def _answered(result: Any) -> None:
        # 🔴 THE SWAP IS INTERCEPTED HERE AND THE CALLBACK NEVER SEES IT. This is
        # what makes the sentinel's falsiness a backstop rather than the
        # mechanism. The contract is not "a swap answers with something
        # harmless" — it is that a swap DOES NOT ANSWER. Esc resolves with None
        # and the callback FIRES; a swap does not reach it at all.
        if isinstance(result, _SwapRequested):
            open_dialog(app, body_factory, callback,
                        style="sidebar", side=app.settings.dialog_side)
            return
        if callback is not None:
            callback(result)

    app.push_screen(_mark_swap_host(modal_factory()), _answered)


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
    if refuse_over_rpc(app, _dialog_name(body_factory)):
        return

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
        # unstyled and changes what the modal IS.
        #
        # ⇒ A SWAP ARRIVES AS THE DISMISSAL VALUE, AND THIS FRAME DOES NOT
        # UNWIND FOR IT. It falls through to the controller below on the sidebar
        # host, so the AWAITING caller — `_execute_tool`, whose very next line is
        # `if not answer:` and treats falsy as DENY-and-stop-the-turn — cannot
        # observe the sentinel: there is no return between here and a real
        # answer.
        result = await app.push_screen_wait(_mark_swap_host(modal_factory()))
        if not isinstance(result, _SwapRequested):
            return result
        style = "sidebar"
    return await DialogController(app, body_factory, style, side, trap_focus).open()
