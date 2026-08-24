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
from textual.screen import ModalScreen
from textual.widget import Widget

#: What a dialog body must offer to survive a swap. Bodies without these still
#: work — the state simply does not carry — so a body is never *required* to
#: implement them, but anything with input in it should.
BodyFactory = Callable[[], Widget]


class DialogController:
    """Owns the future. Views come and go beneath it."""

    def __init__(self, app, body_factory: BodyFactory, style: str) -> None:
        self.app = app
        self.style = style
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
        if self.style == "sidebar":
            view: Any = SidePanel(self, body)
            await self.app.screen.mount(view)
        else:
            view = _ModalHost(self, body)
            self.app.push_screen(view)
        self._view = view

    async def swap(self) -> None:
        """Change style IN PLACE. Must not touch the future."""
        if self._done:
            return
        view, self._view = self._view, None
        if view is not None:
            body = view.body
            try:
                self._state = body.get_state()
            except AttributeError:
                self._state = {}   # a body without get_state simply carries nothing
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

    def _restore_focus_target(self):
        return getattr(self, "_prev_focus", None)

    def _focus_something(self) -> None:
        for node in self.query("*"):
            if node.focusable:
                node.focus()
                return
        try:
            self.focus()
        except Exception:
            pass


class SidePanel(Widget, _ViewMixin):
    """Right-hand split view. Carves space; the chat reflows narrower."""

    DEFAULT_CSS = """
    SidePanel {
        split: right;
        width: 33%;
        min-width: 30;
        max-width: 60;
        border-left: vkey $foreground 30%;
        height: 100%;
        padding: 0 1;
        layout: vertical;
    }
    """

    can_focus = True
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, controller: DialogController, body: Widget) -> None:
        super().__init__()
        self.controller = controller
        self.body = body
        self._prev_focus = None

    def compose(self) -> ComposeResult:
        yield self.body

    def on_mount(self) -> None:
        self._prev_focus = self.screen.focused
        self.controller.apply_carried_state(self.body)
        self._focus_something()

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
        self.controller.apply_carried_state(self.body)
        self._focus_something()

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
    """Answer the dialog containing `widget`. The body's only exit."""
    ctrl = _controller_for(widget)
    if ctrl is not None:
        ctrl.resolve(value)


def request_swap(widget: Widget) -> None:
    """Swap the host under `widget` without answering the dialog."""
    ctrl = _controller_for(widget)
    if ctrl is not None:
        ctrl.app.call_next(ctrl.swap)


async def show_dialog(app, body_factory: BodyFactory, *, style: str | None = None) -> Any:
    """Show a dialog and await its answer, honouring `dialog_style`.

    `body_factory` is called once per host — a swap builds a fresh body and
    replays `get_state()` into it. Returns whatever was passed to
    `close_dialog`, or None if cancelled: the same contract `push_screen_wait`
    already has, so an awaiting caller does not change when the style does.
    """
    if style is None:
        style = app.settings.dialog_style
    return await DialogController(app, body_factory, style).open()
