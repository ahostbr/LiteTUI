"""Hook authoring and explicit script testing, shared by Settings and /hooks."""
from __future__ import annotations

import json
import sys
from typing import ClassVar

from textual import on, work
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TextArea

from litetui import hook_host
from litetui import lifecycle_hooks as hooks
from litetui.side_panel import SwapButton, close_dialog

# One declaration builds and reads the form (the settings theme-token pattern).
HOOK_FIELDS = (
    ("id", "ID", "check"),
    ("events", "Events (comma separated)", "tool_before"),
    ("executable", "Executable", sys.executable),
    ("argv", "Arguments (JSON array)", "[]"),
    ("cwd", "Working directory (blank uses launch workspace)", ""),
    ("env", "Environment overrides (JSON object)", "{}"),
    ("timeout", "Timeout in seconds (1–300)", "10"),
    ("sources", "Sources (comma separated; blank matches all)", ""),
    ("tools", "Tool names or * / ? patterns (comma separated)", ""),
)


class HooksEditor(VerticalScroll):
    DEFAULT_CSS = """
    HooksEditor { height: 1fr; padding: 0 1; }
    HooksEditor Label { height: auto; margin-top: 1; }
    HooksEditor Horizontal { height: auto; min-height: 3; }
    HooksEditor Button { min-width: 9; margin-right: 1; }
    HooksEditor Input, HooksEditor Select { width: 1fr; }
    HooksEditor #hook-error { color: $error; height: auto; }
    HooksEditor #hook-status, HooksEditor #hook-result { height: auto; }
    HooksEditor TextArea { height: 8; }
    """

    def __init__(self):
        super().__init__()
        self.scope = "project"
        self.rows = []
        self.baseline = ()
        self.selected = None
        self._loading = False
        self._broken_bytes = None

    def compose(self):
        yield Static("", id="hook-status")
        yield Label("Edit scope — project IDs override global IDs, including disabled entries")
        yield Select([("Project", "project"), ("Global", "global")], value="project", allow_blank=False, id="hook-scope")
        yield Select([], prompt="Choose a hook", id="hook-select")
        with Horizontal():
            for label, id in (("New", "new"), ("Duplicate", "duplicate"), ("Override global", "override")):
                yield Button(label, id=f"hook-{id}")
        with Horizontal():
            for label, id in (("Delete", "delete"), ("Move up", "up"), ("Move down", "down")):
                yield Button(label, id=f"hook-{id}")
        yield Label("Enabled")
        yield Switch(True, id="hook-enabled")
        yield Label("Mode")
        yield Select([("Observe", "observe"), ("Gate", "gate")], value="observe", allow_blank=False, id="hook-mode")
        yield Static("Available events: " + ", ".join(hooks.EVENTS))
        yield Static("Sources: " + ", ".join(hooks.SOURCES))
        for name, label, default in HOOK_FIELDS:
            yield Label(label)
            yield Input(default, id=f"hook-field-{name}")
        yield Static("", id="hook-error")
        yield TextArea("", id="hook-repair-json")
        yield Button("Save repaired file JSON", id="hook-repair")
        with Horizontal():
            yield Button("Save hooks", variant="primary", id="hook-save")
            yield Button("Reload", id="hook-reload")
        yield Label("Test event JSON — Test executes this script with the normal approval policy")
        yield TextArea(json.dumps({"event": "tool_before", "source": "typed", "data": {"tool": "read", "args": {}}}, indent=2), id="hook-sample")
        yield Button("Test script", variant="warning", id="hook-test")
        yield Static("", id="hook-result")
        yield Label("Rejected prompts remain in the transcript and here until this app exits")
        yield Select([], prompt="Rejected prompt", id="hook-rejected")
        yield Button("Resubmit selected prompt", id="hook-resubmit")

    def on_mount(self):
        if not hasattr(self.app, "hook_config"):
            self.disabled = True
            self.error("Hook controls require the LiteTUI host.")
            return
        self.reload()

    def error(self, text):
        self.query_one("#hook-error", Static).update(text)

    def reload(self):
        self.query_one("#hook-repair-json").display = False
        self.query_one("#hook-repair").display = False
        self._broken_bytes = None
        try:
            self.baseline = self.app.hook_config.read(self.scope)
            self.rows = list(self.baseline)
            self.selected = self.rows[0].id if self.rows else None
            self.refresh_rows()
            self.load_form()
            self.error("")
        except (hooks.HookError, OSError) as exc:
            self.error(str(exc))
            try:
                self._broken_bytes = self.app.hook_config.path(self.scope).read_bytes()
            except OSError:
                return
            self.query_one("#hook-repair-json", TextArea).load_text(self._broken_bytes.decode("utf-8-sig", errors="replace"))
            self.query_one("#hook-repair-json").display = True
            self.query_one("#hook-repair").display = True

    def refresh_rows(self):
        self._loading = True
        selector = self.query_one("#hook-select", Select)
        selector.set_options([(f"{h.id} · {h.mode} · {'enabled' if h.enabled else 'disabled'}", h.id) for h in self.rows])
        selector.value = self.selected if self.selected else Select.NULL
        state = self.app.hook_config.snapshot()
        effective = "; ".join(f"{h.scope}/{h.id} ({'enabled' if h.enabled else 'disabled'})" for h in state.hooks)
        self.query_one("#hook-status", Static).update(
            f"Global: {self.app.hook_config.global_path}\nProject: {self.app.hook_config.project_path}\n"
            + ("Hooks OFF (LITETUI_HOOKS=off)" if state.disabled else state.error or "Effective: " + (effective or "none")))
        rejected = getattr(self.app, "rejected_prompts", [])
        self.query_one("#hook-rejected", Select).set_options([
            (f"{i + 1}: {str(p['content'])[:90]} — {p['reason'][:90]}", i)
            for i, p in enumerate(rejected)])
        self._loading = False

    def load_form(self):
        hook = next((h for h in self.rows if h.id == self.selected), None)
        if hook is None:
            return
        self._loading = True
        data = hook.document()
        for name, _, _ in HOOK_FIELDS:
            value = data[name]
            if name in ("argv", "env"):
                value = json.dumps(value)
            elif isinstance(value, (list, tuple)):
                value = ", ".join(value)
            self.query_one(f"#hook-field-{name}", Input).value = str(value if value is not None else "")
        self.query_one("#hook-enabled", Switch).value = hook.enabled
        self.query_one("#hook-mode", Select).value = hook.mode
        result = self.app.hook_results.get((self.scope, hook.id))
        self.query_one("#hook-result", Static).update(str(result) if result else "Not run this session")
        self._loading = False

    def read_form(self):
        data = {name: self.query_one(f"#hook-field-{name}", Input).value for name, _, _ in HOOK_FIELDS}
        for name in ("events", "sources", "tools"):
            data[name] = [s.strip() for s in data[name].split(",") if s.strip()]
        for name in ("argv", "env"):
            data[name] = json.loads(data[name])
        data["timeout"] = float(data["timeout"])
        data["cwd"] = data["cwd"] or None
        data["enabled"] = self.query_one("#hook-enabled", Switch).value
        data["mode"] = self.query_one("#hook-mode", Select).value
        return hooks.Hook.parse(data, self.scope)

    def keep_form(self):
        if self.selected is None:
            return
        hook = self.read_form()
        if hook.id != self.selected and any(h.id == hook.id for h in self.rows):
            raise hooks.HookError("duplicate hook ID")
        self.rows = [hook if h.id == self.selected else h for h in self.rows]
        self.selected = hook.id

    @on(Select.Changed, "#hook-scope")
    def scope_changed(self, event):
        if str(event.value) != self.scope:
            self.scope = str(event.value)
            self.reload()

    @on(Select.Changed, "#hook-select")
    def selection_changed(self, event):
        if self._loading or event.value == Select.NULL or event.value == self.selected:
            return
        try:
            self.keep_form()
            self.selected = str(event.value)
            self.load_form()
        except (ValueError, hooks.HookError) as exc:
            self.error(str(exc))

    @on(Button.Pressed)
    def pressed(self, event):
        id = event.button.id or ""
        if not id.startswith("hook-"):
            return
        event.stop()
        try:
            if id == "hook-repair":
                self.app.hook_config.repair(self.scope,
                    self.query_one("#hook-repair-json", TextArea).text, self._broken_bytes)
                self.reload()
                return
            if id == "hook-reload":
                self.reload()
                return
            if id == "hook-test":
                self.test_script()
                return
            if id == "hook-resubmit":
                selected = self.query_one("#hook-rejected", Select).value
                if selected != Select.NULL:
                    item = self.app.rejected_prompts[int(selected)]
                    self.app._pending_input.append(dict(item))
                    self.app._flush_pending_input()
                return
            self.keep_form()
            if id in ("hook-new", "hook-duplicate", "hook-override"):
                if id == "hook-override":
                    if self.scope != "global" or not self.selected:
                        raise hooks.HookError("Select a global hook, then create its project override")
                    data = self.read_form().document()
                    self.scope = "project"
                    self.query_one("#hook-scope", Select).value = "project"
                    self.reload()
                else:
                    data = self.read_form().document() if id == "hook-duplicate" else {
                        "id": "new-hook", "events": ["tool_before"], "executable": sys.executable, "argv": []}
                    base = data["id"]
                    n = 2
                    while any(h.id == data["id"] for h in self.rows):
                        data["id"] = f"{base}-{n}"
                        n += 1
                hook = hooks.Hook.parse(data, self.scope)
                self.rows = [h for h in self.rows if h.id != hook.id] + [hook]
                self.selected = hook.id
            elif id == "hook-delete":
                self.rows = [h for h in self.rows if h.id != self.selected]
                self.selected = self.rows[0].id if self.rows else None
            elif id in ("hook-up", "hook-down") and self.selected:
                index = next(i for i, h in enumerate(self.rows) if h.id == self.selected)
                other = max(0, min(len(self.rows) - 1, index + (-1 if id == "hook-up" else 1)))
                self.rows[index], self.rows[other] = self.rows[other], self.rows[index]
            elif id == "hook-save":
                self.baseline = self.app.hook_config.save(self.scope, self.rows, self.baseline)
                self.rows = list(self.baseline)
            self.refresh_rows()
            self.load_form()
            self.error("Saved; applies to the next event" if id == "hook-save" else "Unsaved changes")
        except (ValueError, OSError, hooks.HookError) as exc:
            self.error(str(exc))

    @work(group="hook-test", exclusive=True)
    async def test_script(self):
        try:
            hook = self.read_form()
            sample = json.loads(self.query_one("#hook-sample", TextArea).text)
            if not isinstance(sample, dict) or sample.get("event") not in hooks.EVENTS:
                raise hooks.HookError("sample requires a supported event")
            document = hooks.event_document(sample["event"], self.app._hook_workspace,
                                            sample.get("data", {}), source=sample.get("source", "typed"))
            result = await hook_host.invoke(self.app, hook, document, self.app.chosen_tool_profile, testing=True)
            self.query_one("#hook-result", Static).update(
                f"{'allow/success' if result.allowed else 'denied/failed'}: {result.reason}\n"
                f"Exit: {result.exit_code}; {result.seconds:.2f}s; timeout={result.timed_out}; truncated={result.truncated}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        except (ValueError, OSError, hooks.HookError) as exc:
            self.error(str(exc))


class HooksBody(VerticalScroll):
    DEFAULT_CSS = "HooksBody { height: 1fr; min-height: 15; } HooksBody > Horizontal { height: 3; }"

    def compose(self):
        yield HooksEditor()
        with Horizontal():
            yield Button("Close", id="hooks-close")
            yield SwapButton()

    @on(Button.Pressed, "#hooks-close")
    def close(self):
        close_dialog(self, None)


class HooksScreen(ModalScreen):
    DEFAULT_CSS = "HooksScreen { align: center middle; } HooksScreen > HooksBody { width: 90%; height: 90%; background: $surface; border: round $primary; }"
    BINDINGS: ClassVar = [("escape", "close", "Close")]

    def compose(self):
        yield HooksBody()

    def action_close(self):
        self.dismiss(None)
