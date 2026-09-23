"""The `/settings` screen — every knob, grouped, scrollable, editable.

DESIGN NOTES

* SCROLLABLE BY CONSTRUCTION. The body is a `VerticalScroll` and every group
  lives inside it. A settings panel that outgrows the terminal and cannot be
  scrolled hides its own contents, and the knobs that get hidden are the ones
  added last — which are the ones nobody has tried yet.

* ENV-LOCKED FIELDS ARE DISABLED, NOT SILENTLY OVERWRITTEN. Precedence is
  env > file > default, so a field the environment is currently supplying
  cannot be changed here — the env would win it straight back. Rendering an
  editable control for it would be a control that lies, so those render
  disabled with the variable name shown.

* NOTHING IS APPLIED UNTIL SAVE. Edits accumulate in a working copy. Cancel
  discards. This matters because several knobs (context length, model) trigger
  work on apply, and a half-typed number must never reach the server.

* INVALID INPUT IS REFUSED AT SAVE, NAMED BY FIELD. A settings screen that
  accepts "abc" for a token budget and quietly keeps the old value teaches you
  that the control does not work.
"""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from dataclasses import fields, replace
from functools import partial
from typing import Any

from litetui.friendly_errors import present

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TabbedContent,
    TabPane,
)

# THE MODULE, not the names. `from ... import PROFILES` binds at import
# time, which would make the "derivation" a snapshot: a profile added
# later would not appear, and the test proving it appears could only pass
# by patching THIS module -- i.e. by touching the screen, which is the
# exact thing the derivation exists to stop being necessary.
from litetui import gpu_gate, llm_backend, stt_backend, tool_policy, voice_backend
from litetui import settings as settings_mod
from litetui.colorpicker import ColorPickerBody, ColorPickerScreen
from litetui.hooks_screen import HooksEditor
from litetui.settings import Settings
from litetui.settings_apply import SettingsSaveResult
from litetui.settings_draft import SettingsDraft
from litetui.settings_ui_adapter import (
    RuntimeApply,
    SavePatch,
    SettingsConflictError,
    SettingsUiAdapter,
    SnapshotProvider,
    persistence_error,
)
from litetui.settings_ui_model import (
    SETTINGS_SECTIONS,
    SettingSearchHit,
    SettingsSectionSpec,
    search_settings,
)
from litetui.side_panel import SwapButton, close_dialog, present_dialog

THINKING_CHOICES = [
    ("off — no reasoning (may be ignored, see docs)", "off"),
    ("minimal", "minimal"),
    ("low", "low"),
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh — most expensive", "xhigh"),
]

def _theme_choices(custom: dict | None = None):
    """Built-ins (lights stripped) + the LiteSuite ports + the shades + any
    custom themes, computed at call time — a module-level constant missed
    every theme created after import."""
    from textual.theme import BUILTIN_THEMES

    from litetui import themes as themes_mod
    names = [n for n in BUILTIN_THEMES if n not in themes_mod.LIGHT_BUILTINS] + [
        n for n in themes_mod.ALL_THEMES if n not in BUILTIN_THEMES
    ] + [n for n in (custom or {}) if n not in themes_mod.ALL_THEMES]
    return [(n, n) for n in names]

TOOL_CONTEXT_CHOICES = [
    ("off — raw output enters context (baseline)", "off"),
    ("llm-tool-mask — pointer placeholder, no model call", "llm-tool-mask"),
    ("llm-tool-summ — side-call summary toward the task", "llm-tool-summ"),
]

def tool_profile_choices() -> list[tuple[str, str]]:
    """The profile dropdown, DERIVED from `tool_policy.PROFILES`.

    🔴 THIS USED TO BE A HAND-WRITTEN LIST, and that let the UI's set of
    profiles drift from the engine's in a direction nothing caught: a profile
    added to `PROFILES` but not here EXISTS, is rejected by the validator at
    the bottom of this file, and cannot be selected by anyone. Nothing went
    red. The reverse drift was loud, so only the silent half ever bit.

    A FUNCTION, not a module constant, because a constant is computed once at
    import and a test cannot then add a profile and watch it appear — which is
    the property that makes the drift impossible rather than merely fixed.
    """
    return [
        (
            f"{name} — {tool_policy.PROFILES[name].summary}"
            if tool_policy.PROFILES[name].summary
            else name,
            name,
        )
        # SELECTABLE, not every profile: T085 took `scheduled` out of the
        # user-facing set ("scheduled should not be its own mode") while
        # keeping it as the floor `unattended()` degrades to. Derived from
        # `ToolProfile.selectable`, so this dropdown and shift+tab read the
        # SAME source and cannot offer different sets.
        for name in tool_policy.selectable_profile_names()
    ]

ERROR_MESSAGE_CHOICES = [
    ("Plain Talk — short explanations and next steps", "plain"),
    ("Full Detail — original diagnostic messages", "detail"),
]

DIALOG_STYLE_CHOICES = [
    ("modal — dialog covers the chat (current)", "modal"),
    ("sidebar — dialog splits off the side, chat stays readable", "sidebar"),
]

# Two axes, two controls, deliberately NOT folded into one four-state select.
# "modal on the left" is not a state, so a combined control would have to either
# offer it or explain why it is missing.
DIALOG_SIDE_CHOICES = [
    ("right (default)", "right"),
    ("left", "left"),
]


class _Row(Horizontal):
    """One label + control + help line."""


class SettingsSectionHeader(Button):
    """A mounted, non-destructive disclosure header for one settings group.

    The controls remain mounted when folded. That preserves `_collect`'s
    whole-form safety check and lets search reveal a field without rebuilding
    or mutating the draft.
    """

    def __init__(self, spec: SettingsSectionSpec):
        self.spec = spec
        self._members: list[Widget] = []
        self._open = spec.default_expanded
        super().__init__(
            self._label(),
            id=f"set-section-{spec.section_id}",
            classes="settings-section-toggle",
        )

    def _label(self) -> str:
        arrow = "▾" if self._open else "▸"
        count = f"{len(self.spec.fields)} controls" if self.spec.fields else "advanced editor"
        scopes = ", ".join(dict.fromkeys(field.scope for field in self.spec.fields))
        scope = f" · {scopes}" if scopes else ""
        return f"{arrow} {self.spec.title}  ·  {count}{scope}"

    def on_mount(self) -> None:
        self.call_after_refresh(self._capture_members)

    def _capture_members(self) -> None:
        parent = self.parent
        if parent is None:
            return
        siblings = list(parent.children)
        try:
            start = siblings.index(self) + 1
        except ValueError:
            return
        members: list[Widget] = []
        for sibling in siblings[start:]:
            if isinstance(sibling, SettingsSectionHeader):
                break
            members.append(sibling)
        self._members = members
        self._set_open(self._open)

    def _set_open(self, value: bool) -> None:
        self._open = value
        self.label = self._label()
        for member in self._members:
            member.display = value

    def toggle(self) -> None:
        self._set_open(not self._open)

    def apply_search(self, query: str, matching_fields: tuple[str, ...], section_match: bool) -> None:
        if not query:
            self.display = True
            for member in self._members:
                member.display = True
            self._set_open(self.spec.default_expanded)
            return
        self.display = section_match
        if not section_match:
            for member in self._members:
                member.display = False
            return
        self._set_open(True)
        if not matching_fields:
            return
        wanted = set(matching_fields)
        for member in self._members:
            ids = {
                node.id[2:]
                for node in member.query("*")
                if node.id and node.id.startswith("f-")
            }
            member.display = not ids or bool(ids & wanted)

    def first_matching_widget(self, field_names: tuple[str, ...]) -> Widget | None:
        wanted = set(field_names)
        for member in self._members:
            for node in member.query("*"):
                if node.id and node.id.startswith("f-") and node.id[2:] in wanted:
                    return node
        return None


def _num_or_none(raw: str, cast) -> Any:
    raw = raw.strip()
    if raw == "":
        return None
    return cast(raw)


def _validate_tts_timeout(value: int) -> None:
    """Reject a disabled TTS timeout before it reaches the voice runtime."""

    if value < 1:
        raise ValueError("tts_timeout: must be at least 1 second")


def _tts_timeout_from_input(raw: str) -> int:
    """Parse the live Voice control used by the Test voice action."""

    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError("tts_timeout: enter a whole number of seconds") from exc
    _validate_tts_timeout(value)
    return value


#: Declared once and installed on both the body and its screen; see
#: `scheduler_ui._CAL_KEYS`. `escape` is on the screen only — `SidePanel`
#: already binds it to cancel the dialog.
_SET_KEYS = [Binding("ctrl+s", "save", "Save", show=False)]


class SettingsExitConfirm(ModalScreen[str]):
    """Resolve a dirty close or an explicit Restore-defaults request."""

    DEFAULT_CSS = """
    SettingsExitConfirm { align: center middle; }
    #settings-exit-box { width: 76; height: auto; padding: 2; background: $panel; border: tall $primary; }
    #settings-exit-buttons { height: auto; align: center middle; }
    #settings-exit-buttons Button { margin: 1; }
    """

    BINDINGS = [Binding("escape", "keep_editing", "Keep editing", show=False)]

    def __init__(self, *, restore: bool = False):
        super().__init__()
        self.restore = restore

    def compose(self) -> ComposeResult:
        with Vertical(id="settings-exit-box"):
            if self.restore:
                yield Static("Restore all settings to defaults?", id="settings-exit-title")
                yield Static(
                    "This replaces the current draft and applies only after you confirm.",
                    id="settings-exit-sub",
                )
                with Horizontal(id="settings-exit-buttons"):
                    yield Button("Restore defaults", variant="warning", id="settings-restore")
                    yield Button("Keep editing", id="settings-keep")
            else:
                yield Static("Unsaved settings changes", id="settings-exit-title")
                yield Static(
                    "Save the draft, discard it, or return to the settings panel.",
                    id="settings-exit-sub",
                )
                with Horizontal(id="settings-exit-buttons"):
                    yield Button("Save", variant="primary", id="settings-save")
                    yield Button("Discard", variant="error", id="settings-discard")
                    yield Button("Keep editing", id="settings-keep")

    def action_keep_editing(self) -> None:
        self.dismiss("keep")

    @on(Button.Pressed, "#settings-save")
    def _save(self) -> None:
        self.dismiss("save")

    @on(Button.Pressed, "#settings-discard")
    def _discard(self) -> None:
        self.dismiss("discard")

    @on(Button.Pressed, "#settings-restore")
    def _restore(self) -> None:
        self.dismiss("restore")

    @on(Button.Pressed, "#settings-keep")
    def _keep(self) -> None:
        self.dismiss("keep")


def loop_model_choices(
    models,
    loaded,
    remote: bool,
    sentinel_label: str,
    current: str | None,
):
    """Options for an Agent-loop model picker: (label, value) pairs.

    🔴 IT IS A FUNCTION SO THE LIST CAN BE ASSERTED (T640). Built inline in
    `compose` the contract would be pinned by nothing, and the one property that
    matters here — the options are EXACTLY what the backend reports, never a
    hand-kept list — is the kind that rots silently when a backend gains a model
    kind nobody updated a literal for.

    ⚠️ AND THE PERSISTED VALUE IS ALWAYS AN OPTION, EVEN WHEN THE SERVER HAS
    NEVER HEARD OF IT. The Model tab learned this the hard way: Textual REFUSES
    a `Select` value that is not among its options and the WHOLE PANEL fails to
    open — and since models are discovered asynchronously, "not in the list yet"
    is the normal state for the first moment of every launch. A settings screen
    that cannot open is a worse bug than a stale option.

    Resident first, because on a local backend that is the only set a side call
    may use without loading something; the rest are shown and MARKED rather than
    hidden, so a model you have downloaded but not loaded looks different from
    one you do not have at all.
    """
    loaded_set = {m for m in (loaded or ()) if m}
    everything = [m for m in (models or ()) if m]
    out = [(sentinel_label, "")]
    if remote:
        # A remote backend loads nothing, so residency is not a property it has
        # and marking everything "not loaded" would be a lie about the engine.
        out += [(m, m) for m in everything]
    else:
        out += [(f"{m}  (loaded)", m) for m in everything if m in loaded_set]
        out += [
            (f"{m}  (downloaded, not loaded)", m)
            for m in everything
            if m not in loaded_set
        ]
    have = {v for _, v in out}
    if current and current not in have:
        out.append((f"{current}  (not currently served)", current))
    return out



class SettingsBody(Widget):
    """Every knob, grouped and scrollable — host-agnostic.

    Exits through `close_dialog`, so the ANSWER is unchanged in both hosts: the
    new `Settings` on save, `None` on cancel, and a fresh `Settings()` from
    Restore defaults. `app._on_settings_saved` is the only consumer and it does
    not learn that a second host exists.

    `height: 100%` and `#set-box` keeps its own `height: 88%`: that box is
    SHARED with the model panel, so a body that is exactly the screen leaves the
    percentage meaning what it always meant. See ModelConfigBody.
    """

    DEFAULT_CSS = """
    SettingsBody { width: 100%; height: 100%; align: center middle; layout: vertical; }
    SettingsSectionHeader {
        width: 100%;
        height: auto;
        min-height: 3;
        margin: 1 0 0 0;
        padding: 0 1;
        background: $panel;
        color: $primary-lighten-2;
        border: tall $primary-darken-2;
        content-align: left middle;
    }
    SettingsSectionHeader:hover { background: $panel-lighten-1; }
    #set-search-row { height: auto; align: left middle; padding-bottom: 1; }
    #set-search { width: 1fr; }
    #set-search-clear { margin-left: 1; }
    #set-search-status { width: 1fr; color: $text-muted; padding-left: 1; }
    """

    BINDINGS = list(_SET_KEYS)

    def __init__(self, current: Settings, models: list[str] | None = None,
                 mcp_servers: list[str] | None = None,
                 loaded: list[str] | None = None, remote: bool = False,
                 *, snapshot_provider: SnapshotProvider | None = None,
                 save_patch: SavePatch | None = None,
                 runtime_apply: RuntimeApply | None = None):
        super().__init__()
        self._start = current
        self._draft = SettingsDraft(current)
        self._settings_adapter = SettingsUiAdapter(
            current,
            snapshot_provider=snapshot_provider,
            save_patch=save_patch,
            runtime_apply=runtime_apply,
        )
        self._last_save_result: SettingsSaveResult | None = None
        self._initial_state: dict = {}
        self._models = models or []
        self._mcp_servers = mcp_servers or []
        # T640: which of `models` are resident, and whether residency is even a
        # property this backend has. Defaulted so every existing construction
        # (a dozen suites) keeps working and simply shows nothing as loaded.
        self._loaded = loaded or []
        self._remote = remote

    def on_mount(self) -> None:
        # The form is the working copy. Capture its mounted values so an Esc
        # after a partial/invalid edit still asks before discarding it.
        self.call_after_refresh(self._capture_initial_state)

    def _capture_initial_state(self) -> None:
        self._initial_state = self._canonical_state(self.get_state())

    @staticmethod
    def _canonical_state(state: dict) -> dict:
        """Remove view-only state and normalize hook JSON field values."""
        out = deepcopy(state)
        hooks_state = out.get("_hooks_editor")
        if isinstance(hooks_state, dict):
            hooks_state.pop("messages", None)
            hooks_state.pop("scroll_y", None)
            values = hooks_state.get("values")
            if isinstance(values, dict):
                for key, value in list(values.items()):
                    if not isinstance(value, (str, bool, int, float, type(None))):
                        values[key] = repr(value)
        return out

    def _has_unsaved_changes(self) -> bool:
        return bool(self._initial_state) and self._canonical_state(self.get_state()) != self._initial_state

    def request_cancel(self) -> None:
        if not self._has_unsaved_changes():
            close_dialog(self, None)
            return
        self.app.push_screen(SettingsExitConfirm(), self._on_cancel_answer)

    def _on_cancel_answer(self, answer: str | None) -> None:
        if answer == "save":
            self.action_save()
        elif answer == "discard":
            close_dialog(self, None)

    def _section_header(self, section_id: str) -> SettingsSectionHeader:
        spec = next(
            section for section in SETTINGS_SECTIONS
            if section.section_id == section_id
        )
        return SettingsSectionHeader(spec)

    def _apply_search(self, query: str) -> tuple[SettingSearchHit, ...]:
        hits = search_settings(query)
        by_section = {hit.section_id: hit for hit in hits}
        for header in self.query(SettingsSectionHeader):
            hit = by_section.get(header.spec.section_id)
            header.apply_search(
                query,
                hit.field_names if hit else (),
                hit is not None,
            )

        status = self.query_one("#set-search-status", Static)
        if not query.strip():
            status.update("Search labels, help, scopes, and section names")
            return hits
        if not hits:
            status.update("No settings match")
            return hits

        mounted_hits = tuple(
            hit for hit in hits
            if self.query(f"#set-section-{hit.section_id}")
        )
        if not mounted_hits:
            status.update("No visible settings match on this machine")
            return hits

        first = mounted_hits[0]
        self.query_one(TabbedContent).active = f"tab-{first.tab_id}"
        field_count = sum(len(hit.field_names) for hit in mounted_hits)
        status.update(
            f"{len(mounted_hits)} section{'s' if len(mounted_hits) != 1 else ''} · "
            f"{field_count} direct field match{'es' if field_count != 1 else ''}"
        )

        def focus_hit() -> None:
            header = self.query_one(
                f"#set-section-{first.section_id}", SettingsSectionHeader
            )
            header.scroll_visible()
            if first.field_names:
                widget = header.first_matching_widget(first.field_names)
                if widget is not None:
                    widget.focus()

        self.call_after_refresh(focus_hit)
        return hits

    @on(Input.Changed, "#set-search")
    def _search_changed(self, event: Input.Changed) -> None:
        self._apply_search(event.value)

    @on(Button.Pressed, "#set-search-clear")
    def _search_clear(self) -> None:
        field = self.query_one("#set-search", Input)
        field.value = ""
        field.focus()

    @on(Button.Pressed, ".settings-section-toggle")
    def _section_toggle(self, event: Button.Pressed) -> None:
        if isinstance(event.button, SettingsSectionHeader):
            event.button.toggle()
            event.stop()

    # ── Builders ─────────────────────────────────────────────────────────────

    def _backend_control(self, name):
        from litetui.codex_settings import control

        return control(getattr(self.app, "backend", None), name)

    def _text_row(self, name: str, label: str, help_text: str, placeholder: str = ""):
        locked = settings_mod.source_of(name)
        capability = self._backend_control(name)
        if capability:
            help_text = capability.help
        value = getattr(self._start, name)
        shown = "" if value is None else (
            ",".join(value) if isinstance(value, list) else str(value)
        )
        with Vertical(classes="set-row"):
            yield Label(label, classes="set-label")
            yield Input(
                value=shown,
                placeholder=placeholder or "unset — server default",
                id=f"f-{name}",
                disabled=locked is not None or bool(capability and not capability.editable),
                classes="set-input",
            )
            note = help_text
            if locked:
                note = f"LOCKED by ${locked} — unset it to edit here.  {help_text}"
            yield Static(note, classes="set-help")

    def _switch_row(self, name: str, label: str, help_text: str):
        capability = self._backend_control(name)
        if capability:
            help_text = capability.help
        with Vertical(classes="set-row"):
            with Horizontal(classes="set-switchline"):
                yield Switch(value=bool(getattr(self._start, name)), id=f"f-{name}",
                             disabled=bool(capability and not capability.editable))
                yield Label(label, classes="set-label-inline")
            yield Static(help_text, classes="set-help")

    def _thinking_choices(self, name="thinking_level"):
        backend = getattr(self.app, "backend", None)
        if getattr(backend, "name", "") == "codex":
            choices = [(level.title() if level != "xhigh" else "Extra high", level)
                       for level in backend.reasoning_levels(self.app.model_id)]
            current = getattr(self._start, name)
            if current not in [value for _, value in choices]:
                choices.append((f"{current} (saved; not supported by this model)", current))
            return choices
        return THINKING_CHOICES

    def _select_row(self, name: str, label: str, choices, help_text: str):
        locked = settings_mod.source_of(name)
        capability = self._backend_control(name)
        if capability:
            help_text = capability.help
        with Vertical(classes="set-row"):
            yield Label(label, classes="set-label")
            yield Select(
                choices,
                value=getattr(self._start, name),
                id=f"f-{name}",
                allow_blank=False,
                disabled=locked is not None or bool(capability and not capability.editable),
            )
            note = f"LOCKED by ${locked}.  {help_text}" if locked else help_text
            yield Static(note, classes="set-help")

    def _model_pick_row(self, name: str, label: str, sentinel: str, help_text: str):
        """One picker, two fields (T640) — the subagent's and the fold's."""
        locked = settings_mod.source_of(name)
        capability = self._backend_control(name)
        if capability:
            help_text = capability.help
        current = getattr(self._start, name)
        choices = loop_model_choices(
            self._models, self._loaded, self._remote, sentinel, current
        )
        with Vertical(classes="set-row"):
            yield Label(label, classes="set-label")
            yield Select(
                choices,
                value=current or "",
                id=f"f-{name}",
                allow_blank=False,
                disabled=locked is not None,
            )
            note = f"LOCKED by ${locked}.  {help_text}" if locked else help_text
            yield Static(note, classes="set-help")

    def compose(self) -> ComposeResult:
        with Vertical(id="set-box"):
            yield Static("Settings", id="set-title")
            yield Static(
                "Esc cancels · Ctrl+S saves · ←/→ or click to change tab",
                id="set-sub",
            )
            with Horizontal(id="set-search-row"):
                yield Input(
                    placeholder="Find a setting, help text, or section…",
                    id="set-search",
                )
                yield Static("", id="set-search-status")
                yield Button("Clear", id="set-search-clear")
            # One tab per section. Each pane scrolls on its own, so no section
            # can push another off the bottom.
            with TabbedContent(id="set-tabs"):
                with TabPane("Model", id="tab-model"):
                    with VerticalScroll(classes="set-scroll"):

                        yield self._section_header("model-connection")
                        # The configured default MUST appear as an option even
                        # when the server is not serving it, or Textual refuses
                        # the value and the WHOLE PANEL fails to open. Models are
                        # discovered asynchronously, so "not in the list" is the
                        # normal state for the first moment of every launch —
                        # this crashed intermittently and read as haunted.
                        model_choices = [("(whatever LM Studio has loaded)", "")] + [
                            (m, m) for m in self._models
                        ]
                        _cur = self._start.default_model
                        if _cur and _cur not in self._models:
                            model_choices.append((f"{_cur}  (not currently served)", _cur))
                        locked = settings_mod.source_of("default_model")
                        with Vertical(classes="set-row"):
                            yield Label("Default model", classes="set-label")
                            yield Select(
                                model_choices,
                                value=self._start.default_model or "",
                                id="f-default_model",
                                allow_blank=False,
                                disabled=locked is not None,
                            )
                            yield Static(
                                (f"LOCKED by ${locked}.  " if locked else "")
                                + "Selected on connect when the server is serving it.",
                                classes="set-help",
                            )
                        yield from self._switch_row(
                            "pin_default_model",
                            "Re-apply on every connect",
                            "Off = only pick it when nothing is loaded. On = always switch back to it.",
                        )
                        yield from self._text_row(
                            "default_context_length",
                            "Load with context length",
                            "Tokens. Blank leaves the server's own configured length alone. "
                            "A larger window costs VRAM and can drop tok/s sharply.",
                            placeholder="e.g. 131072",
                        )
                        yield from self._text_row(
                            "lm_host", "LM Studio host",
                            "Scheme + host + port, no trailing path.",
                            placeholder="http://localhost:1234",
                        )
                        yield from self._text_row(
                            "lmstudio_graded_thinking_models",
                            "Graded thinking works on (LM Studio)",
                            "Comma-separated model ids. On LM Studio a graded level is "
                            "silently DROPPED by a model with no reasoning-level mapping, "
                            "so levels collapse to on/off except for the ids listed here. "
                            "Matched exactly — use the id LM Studio serves the model under.",
                            placeholder="qwen/qwen3.8-27b",
                        )

                        # ── Backend (engine selection) ───────────────────────
                        yield self._section_header("model-engine")
                        yield from self._select_row(
                            "backend", "Engine",
                            [(label, name) for name, label in llm_backend.visible_backends()],
                            "Which engine serves the chat. /backend switches "
                            "live; this is the boot default.",
                        )
                        yield from self._switch_row(
                            "codex_native_engine", "Codex: official app-server owns the loop",
                            "Off (default): LiteTUI's own tools, compaction and conversations "
                            "drive Codex over the Responses API with prompt caching. On: the "
                            "official Codex engine owns tools, history and compaction. "
                            "Applies on /reconnect.",
                        )
                        yield self._section_header("model-router")
                        yield from self._text_row(
                            "custom_base_url", "Custom server URL",
                            "OpenAI-compatible endpoint. No fallback or automatic model loading.",
                            placeholder="http://127.0.0.1:1235/v1",
                        )
                        yield from self._text_row(
                            "custom_api_key_env", "Custom API key environment variable",
                            "Optional environment variable NAME containing the credential; never paste a key here.",
                        )
                        yield from self._text_row(
                            "custom_context_length", "Custom context budget",
                            "Declared client budget in tokens; 0 means unknown. Does not resize the server's context.",
                        )
                        yield from self._text_row(
                            "llama_executable", "llama-server executable",
                            "Blank uses the existing LiteSuite-managed installation; choose another installed executable to override.",
                            placeholder="C:/llama.cpp/llama-server.exe",
                        )
                        yield from self._text_row(
                            "llama_host", "llama.cpp host (ours)",
                            "Where LiteTUI's own router llama-server listens.",
                            placeholder="http://localhost:7470",
                        )
                        yield from self._text_row(
                            "llama_attach_hosts", "Attach to (before spawning)",
                            "Comma-separated. A healthy server here is USED, "
                            "never spawned over — LiteSuite's is :8088.",
                            placeholder="http://localhost:8088",
                        )
                        yield self._section_header("model-discovery")
                        yield from self._switch_row(
                            "llama_scan_litesuite", "Scan LiteSuite models",
                            "~/.litesuite/llm/models — the Model Hub's downloads.",
                        )
                        yield from self._switch_row(
                            "llama_scan_lmstudio", "Scan LM Studio models",
                            "~/.lmstudio/models — served by OUR engine too.",
                        )
                        yield from self._switch_row(
                            "llama_scan_hf_cache", "Scan HuggingFace cache",
                            "$HF_HOME/hub — GGUFs pulled by other tools.",
                        )
                        yield from self._text_row(
                            "llama_models_dirs", "Extra model folders",
                            "Comma-separated absolute paths, scanned recursively.",
                            placeholder="D:/models, E:/gguf",
                        )
                        yield self._section_header("model-loading")
                        yield from self._text_row(
                            "llama_models_max", "Max resident models",
                            "Models loaded at once on our router. Two large "
                            "models already fill a big GPU — raise deliberately.",
                            placeholder="2",
                        )
                        yield from self._text_row(
                            "lms_load_timeout_s", "LM Studio load timeout (s)",
                            "SDK control-plane ceiling; a 20 GB load outlives "
                            "the SDK's 60s default.",
                            placeholder="600",
                        )

                        # ── Generation ───────────────────────────────────────────────
                # T893 — RYAN: "ninfer settings should have their own new tab ... next to
                # [the model tab]", and NOTHING NInfer on a box that is not an RTX 5090.
                # `_collect` skips the ninfer_* fields when the tab is absent.
                if gpu_gate.is_rtx_5090():
                    with TabPane("NInfer", id="tab-ninfer"):
                        with VerticalScroll(classes="set-scroll"):
                            yield self._section_header("ninfer-attach")
                            yield from self._text_row(
                                "ninfer_host", "NInfer host",
                                "Blank discovers the engine LiteSuite started (its config "
                                "registers the port). Set it only for a ninfer-serve you "
                                "started by hand. LiteTUI attaches; it never starts one.",
                                placeholder="http://127.0.0.1:49260",
                            )
                            yield from self._text_row(
                                "ninfer_executable", "NInfer: ninfer-serve executable",
                                "Blank uses LiteSuite's install. /engine start runs this.",
                                placeholder="C:/Users/you/.litesuite/llm/ninfer/ninfer-serve.exe",
                            )
                            yield from self._text_row(
                                "ninfer_artifact", "NInfer: model artifact (.ninfer)",
                                "Blank uses the one file LiteSuite pulled. /engine start serves this.",
                                placeholder="C:/Users/you/.litesuite/llm/ninfer-models/qwen3_8_27b_nvfp4.ninfer",
                            )
                            yield self._section_header("ninfer-envelope")
                            yield from self._text_row(
                                "ninfer_max_context", "NInfer: context length (--max-context)",
                                "Tokens the engine is started with. 32768 is the ruling; larger costs VRAM (fp8 KV).",
                                placeholder="32768",
                            )
                            yield from self._text_row(
                                "ninfer_max_concurrency", "NInfer: concurrent requests (--max-concurrency)",
                                "1..8 requests decoded in one batch — LM Studio's 'Parallel'. They share "
                                "the context-length KV pool: no extra VRAM, less context each under load. "
                                "Applies on the next /engine start; /engine status shows the running value.",
                                placeholder="1",
                            )
                with TabPane("Voice", id="tab-voice"):
                    with VerticalScroll(classes="set-scroll"):
                        yield self._section_header("voice-speak")
                        yield Label("Speak — replies read aloud (TTS out)",
                                    classes="set-label")
                        yield Static("Use Speak / Stop on each response to control playback. Replies are never spoken automatically.")
                        yield from self._select_row(
                            "tts_engine", "TTS engine",
                            [("pyttsx3 — Windows voices, offline, no download", "pyttsx3"),
                             ("edge — Microsoft cloud neural voices (needs install)", "edge")],
                            "pyttsx3 speaks through the built-in Windows voices with "
                            "no download and no network. edge sounds better but calls "
                            "Microsoft and needs the Install button below.")
                        yield from self._select_row(
                            "tts_voice", "pyttsx3 voice",
                            [("System default", "")]
                            + [(v, v) for v in voice_backend.list_sapi_voices()],
                            "The Windows voice pyttsx3 speaks with. Add more in "
                            "Windows Settings > Time & language > Speech.")
                        # OpenBolt owns the Settings field; keep this UI
                        # checkpoint importable before that field lands.
                        if hasattr(self._start, "tts_timeout"):
                            yield from self._text_row(
                                "tts_timeout", "TTS timeout (seconds)",
                                "Maximum time a speech request may run before it is stopped.",
                                placeholder="300")
                        yield from self._text_row(
                            "tts_edge_voice", "edge voice",
                            "The edge-tts voice id used when the engine is 'edge'.",
                            placeholder="en-GB-SoniaNeural")
                        with Vertical(classes="set-row"):
                            with Horizontal(classes="set-switchline"):
                                yield Button("Test voice", id="voice-test")
                                yield Button("Install edge support",
                                             id="voice-install-edge")
                        # ── Dictate (STT in) ──
                        yield Label("Dictate — voice to text (STT in)",
                                    classes="set-label")
                        yield self._section_header("voice-dictate")
                        yield from self._select_row(
                            "stt_model", "Voice-in model (faster-whisper)",
                            [("base.en — ~140 MB, good", "base.en"),
                             ("tiny.en — ~75 MB, snappy", "tiny.en"),
                             ("small.en — ~465 MB, best", "small.en")],
                            "Local speech-to-text for the mic button and the record "
                            "hotkey. Downloaded on first use. Transcript appends to "
                            "the input box.")
                        yield from self._select_row(
                            "stt_mic", "Microphone",
                            [("First available", "")]
                            + [(m, m) for m in stt_backend.list_mics()],
                            "The Direct Show input the recorder captures from.")
                        with Vertical(classes="set-row"):
                            yield Label("Record hotkey", classes="set-label")
                            with Horizontal(classes="set-switchline"):
                                yield Input(value=self._start.stt_hotkey,
                                            id="f-stt_hotkey", classes="set-input",
                                            placeholder="ctrl+space")
                                yield Button("Capture key", id="voice-capture")
                            yield Static(
                                "The key that starts/stops recording from anywhere. "
                                "Type it (e.g. ctrl+space) or click Capture and press "
                                "it. Applies on save.", classes="set-help")
                        with Vertical(classes="set-row"):
                            yield Button("Download voice-in model", id="voice-dl-stt")
                            yield Static("", id="voice-status", classes="set-help")
                with TabPane("Generation", id="tab-generation"):
                    with VerticalScroll(classes="set-scroll"):

                        yield self._section_header("generation-reasoning")
                        yield from self._select_row(
                            "thinking_level", "Thinking level", self._thinking_choices(),
                            "A level THIS SERVER accepts is not always one the LOADED model "
                            "accepts — a virtual model drops an unsupported value with a 200 "
                            "and reasons at its own default instead.",
                        )
                        yield self._section_header("generation-budget")
                        yield from self._text_row(
                            "max_tokens_tools", "Max tokens (tools on)",
                            "Response budget for an agent turn. Reasoning is spent from this "
                            "budget, so a low value can consume the whole allowance thinking "
                            "and emit nothing.",
                        )
                        yield from self._text_row(
                            "max_tokens_chat", "Max tokens (tools off)",
                            "Response budget for a plain chat turn.",
                        )
                        yield self._section_header("generation-sampling")
                        yield from self._text_row(
                            "temperature", "Temperature", "Blank = server default.", "0.0 – 2.0"
                        )
                        yield from self._text_row("top_p", "Top-p", "Nucleus sampling. Blank = default.")
                        yield from self._text_row("top_k", "Top-k", "Blank = default.")
                        yield from self._text_row("min_p", "Min-p", "Blank = default.")
                        yield from self._text_row(
                            "repeat_penalty", "Repeat penalty", "Blank = default."
                        )
                        yield from self._text_row(
                            "presence_penalty", "Presence penalty", "Blank = default."
                        )
                        yield from self._text_row(
                            "frequency_penalty", "Frequency penalty", "Blank = default."
                        )
                        yield self._section_header("generation-repro")
                        yield from self._text_row(
                            "seed", "Seed",
                            "Fixed seed for reproducible output. Blank = random each turn.",
                        )
                        yield from self._text_row(
                            "stop", "Stop strings",
                            "Comma-separated. Blank = none.",
                        )

                        # ── Agent loop ───────────────────────────────────────────────
                with TabPane("Agent loop", id="tab-agent"):
                    with VerticalScroll(classes="set-scroll"):

                        # T640 — THE TWO SIDE CALLS THE LOOP MAKES, TOGETHER.
                        # Both send work to a model that is not the one you are
                        # talking to, and until now one was free text on another
                        # tab and the other did not exist. Ryan 2026-09-11 15:1x
                        # runs MiniCPM5-2B resident beside the big model; these
                        # are the two knobs that point work at it.
                        yield self._section_header("agent-routing")
                        yield from self._model_pick_row(
                            "subagent_model", "Subagent model",
                            "auto — the model you are talking to",
                            "Where the subagent tool's children go. A small model "
                            "loaded beside the big one answers in its own slot, in "
                            "parallel. Only a LOADED local model can be used — nothing "
                            "is loaded to satisfy this.",
                        )
                        yield from self._model_pick_row(
                            "tool_summary_model", "Tool-summary model",
                            "main model — the one you are talking to",
                            "Where the llm-tool-summ fold sends its throwaway side "
                            "call. On Codex the main model means every fold is a Codex "
                            "call; point it at a small local model instead. A pick that "
                            "is not loaded falls back to the main model and says so.",
                        )
                        yield self._section_header("agent-execution")
                        yield from self._switch_row(
                            "tools_enabled", "Tools enabled",
                            "Off = plain chat, no bash/read/write/web_fetch.",
                        )
                        yield from self._text_row(
                            "tool_iterations", "Tool iterations per turn",
                            "The cap behind '[stopped — reached N tool iterations in one "
                            "turn]'. Raise it for long agent runs.",
                        )
                        yield from self._text_row(
                            "tool_auto_background_s", "Auto-background after (seconds)",
                            "A foreground bash/powershell call still running after this "
                            "moves to a background task and the turn carries on; its "
                            "result arrives later as an inbox message. 0 = never.",
                            placeholder="30",
                        )
                        yield from self._switch_row(
                            "enter_interrupts", "Enter interrupts mid-turn",
                            "OFF: Enter queues a mid-turn message; ctrl+shift+enter "
                            "interrupts. ON: the two swap — Enter interrupts, the chord "
                            "queues. Queued messages send when the turn ends; interrupt "
                            "keeps the partial reply and sends yours next.",
                        )
                        yield self._section_header("agent-authority")
                        yield from self._select_row(
                            "tool_policy_profile", "Tool authority",
                            tool_profile_choices(),
                            # RENAMED from "Conversation tool authority": it governs
                            # EVERY turn now -- typed, inbox-woken, cron and loop alike
                            # (Ryan: "cron and loops run at same set profile level").
                            # A control naming a narrower scope than it governs is the
                            # same defect as T084, pointing the other way.
                            "Host-enforced, on every turn: what you type, mail from other "
                            "agents, and cron or loop jobs. Shift+Tab cycles it live and "
                            "the footer always shows the level in force. Interactive asks "
                            "before writes, process execution, desktop control or other "
                            "sensitive effects; scheduled is read-only. A turn nobody is "
                            "watching never opens a prompt -- interactive falls back to "
                            "read-only there rather than waiting for an answer.",
                        )
                        yield from self._text_row(
                            "tool_always_allow", "Never ask again for",
                            "Written by the approval modal's 'Always allow'. Each entry is "
                            "tool:authority, so a rule covers that tool at that authority "
                            "and nothing wider — a bigger request still asks. Clear the "
                            "box to start being asked again.",
                            placeholder="none — every sensitive call asks",
                        )
                        yield from self._text_row(
                            "tool_deny", "Always refuse",
                            "Same tool:authority form. Checked before the profile and "
                            "before any allow rule, so a refusal written here cannot be "
                            "overridden by allowing the same thing.",
                            placeholder="none",
                        )
                        yield self._section_header("agent-tools")
                        yield from self._text_row(
                            "tools_disabled", "Tools switched off",
                            "The boxes you untick in /tools, by tool name. A tool listed "
                            "here has its schema withheld from the model AND is refused "
                            "if called anyway — the second matters because a name it used "
                            "earlier in the same conversation still reaches the host.",
                            placeholder="none — every registered tool is offered",
                        )
                        yield self._section_header("agent-context")
                        yield from self._select_row(
                            "tool_context_mode", "Tool output context", TOOL_CONTEXT_CHOICES,
                            "What a tool result contributes to the conversation. Both "
                            "processing modes park the raw in a sidecar file the model "
                            "can read back — nothing is destroyed. Mask is free; summ "
                            "costs one side call. Measure on your own workload.",
                        )
                        yield from self._text_row(
                            "tool_context_threshold_chars", "Tool context threshold (chars)",
                            "Results smaller than this enter verbatim whatever the mode — "
                            "a summary can be longer than what it replaces.",
                        )
                        # The child residency/keep-warm control will be added to the
                        # Agent loop once its cross-backend runtime contract lands.

                        # ── Compaction ───────────────────────────────────────────────
                with TabPane("Compaction", id="tab-compaction"):
                    with VerticalScroll(classes="set-scroll"):

                        yield self._section_header("compact-trigger")
                        yield from self._switch_row(
                            "autocompact_enabled", "Auto-compact",
                            "Compact by itself once the context window passes the threshold below.",
                        )
                        yield from self._text_row(
                            "autocompact_at_percent", "Auto-compact at (% of window)",
                            "Needs headroom: compaction is itself a request, and one that "
                            "fires at 99% has no room left to write the summary that would "
                            "have saved the session.",
                        )
                        yield from self._switch_row(
                            "wake_after_compact", "Wake model after compacting",
                            "Loop mode: after every successful compaction, send one "
                            "user-style ping so an in-flight task resumes instead of "
                            "the model sitting on the summary. If nothing is pending "
                            "it says standing by and stops.",
                        )
                        yield self._section_header("compact-transcript")
                        yield from self._switch_row(
                            "clear_screen_after_compact", "Clear screen after compacting",
                            "After compacting, the log still shows messages that were just "
                            "REPLACED — the screen and the real context disagree. Clearing "
                            "makes what you can scroll back to match what the model can see.",
                        )
                        yield from self._text_row(
                            "compact_keep_recent", "Keep recent messages",
                            "How many trailing messages survive verbatim after the summary.",
                        )
                        yield self._section_header("compact-summary")
                        yield from self._text_row(
                            "compact_max_tokens", "Compact max tokens",
                            "Budget for the summary itself. This was hardcoded at 2048 and is "
                            "why compaction returned no summary: reasoning consumed the whole "
                            "allowance before any summary was written.",
                        )
                        yield from self._select_row(
                            "compact_thinking_level", "Compact thinking level", self._thinking_choices("compact_thinking_level"),
                            "'off' is cheapest but is the value most likely to be silently "
                            "dropped by a virtual model, which then reasons at ITS default.",
                        )
                        yield from self._text_row(
                            "compact_max_tool_iters", "Compact tool rounds",
                            "How many tool round-trips compaction may take while persisting.",
                        )

                        # ── Capabilities ─────────────────────────────────────────────
                with TabPane("Capabilities", id="tab-capabilities"):
                    with VerticalScroll(classes="set-scroll"):

                        yield self._section_header("cap-identity")
                        yield from self._text_row(
                            "seat_name", "Fleet seat name",
                            "The name this seat asks the LiteHarness registry for, "
                            "and what the footer and `discover` show. Blank = LiteTUI.",
                            placeholder="LiteTUI",
                        )
                        yield self._section_header("cap-skills")
                        yield from self._switch_row(
                            "skills_enabled", "Skills",
                            "Load skills/<name>/SKILL.md. The index goes in the system "
                            "prompt; bodies load on demand.",
                        )
                        yield from self._text_row(
                            "skill_roots", "Extra skill libraries",
                            "Comma-separated directories of skills, scanned after this "
                            "repo's own. ~ expands; * takes the newest match. Only index "
                            "lines are injected — bodies load on demand.",
                            placeholder="~/.claude/skills, ~/.claude/plugins/.../*/skills",
                        )
                        yield self._section_header("cap-mcp")
                        yield from self._switch_row(
                            "mcp_enabled", "MCP servers",
                            "Start the servers declared in mcp.json / .mcp.json.",
                        )
                        if self._mcp_servers:
                            disabled = set(self._start.mcp_disabled_servers)
                            for name in self._mcp_servers:
                                with Vertical(classes="set-row set-indent"):
                                    with Horizontal(classes="set-switchline"):
                                        yield Switch(
                                            value=name not in disabled, id=f"mcp-{name}"
                                        )
                                        yield Label(name, classes="set-label-inline")
                        else:
                            yield Static(
                                "  No MCP servers discovered (looked for mcp.json and .mcp.json).", classes="set-help"
                            )

                        # ── Interface ────────────────────────────────────────────────
                with TabPane("Hooks", id="tab-hooks"):
                    yield self._section_header("hooks-scope")
                    yield Static(
                        "Scope, inventory, and hook actions are exposed by the lifecycle editor below.",
                        classes="set-help",
                    )
                    yield HooksEditor()
                    yield self._section_header("hooks-policy")
                    yield Static(
                        "Policy: enabled, mode, and events/sources/tools stay together in the editor.",
                        classes="set-help",
                    )
                    yield self._section_header("hooks-command")
                    yield Static(
                        "Command contract: ID, executable, arguments, and working directory.",
                        classes="set-help",
                    )
                    yield self._section_header("hooks-runtime")
                    yield Static(
                        "Runtime envelope: environment, timeout, and execution order.",
                        classes="set-help",
                    )
                    yield self._section_header("hooks-test")
                    yield Static(
                        "Test and recovery: repair, save/reload, sample events, and rejected prompts.",
                        classes="set-help",
                    )
                with TabPane("Themes", id="tab-themes"):
                    with VerticalScroll(classes="set-scroll"):
                        yield self._section_header("theme-active")
                        yield from self._select_row(
                            "theme_name", "Theme", _theme_choices(self._start.custom_themes),
                            "Dark built-ins, the LiteSuite ports (matrix, lite-suite, "
                            "amber-ledger...), the ten-gray SHADES, and your customs. "
                            "the footer's \u2630 commands button still has a quick-select; either way the pick "
                            "survives a restart.",
                        )
                        yield self._section_header("theme-custom")
                        yield Static("CREATE / EDIT A CUSTOM THEME", classes="set-subhead")
                        yield Static(
                            "Fields are prefilled from the CURRENT theme, so start by "
                            "picking the nearest neighbour, then nudge. Name + Ctrl+S "
                            "creates it, selects it, and persists it. An existing "
                            "custom name is OVERWRITTEN - the creator is the editor. "
                            "Leave the name empty to save settings without creating.",
                            classes="set-help",
                        )
                        yield self._section_header("theme-tokens")
                        yield from self._custom_theme_rows()

                with TabPane("Interface", id="tab-interface"):
                    with VerticalScroll(classes="set-scroll"):

                        yield self._section_header("interface-transcript")
                        yield from self._switch_row(
                            "show_thinking", "Show thinking blocks",
                            "Render the model's reasoning trace in the transcript.",
                        )
                        yield from self._switch_row(
                            "show_stop_line", "Show turn stop line",
                            "Add elapsed time and final generation speed below the "
                            "last assistant bubble.",
                        )
                        yield from self._switch_row(
                            "show_stop_time", "Show local completion time",
                            "Append the local 12-hour wall-clock time to the turn stop line.",
                        )
                        yield from self._switch_row(
                            "autoscroll", "Follow output",
                            "Keep the log pinned to the newest message while streaming.",
                        )
                        yield from self._select_row(
                            "error_message_style", "Error messages", ERROR_MESSAGE_CHOICES,
                            "Plain Talk explains known issues; Full Detail shows original diagnostics. "
                            "Simplified system lines are also saved in the runtime error log.",
                        )
                        yield self._section_header("interface-dialogs")
                        yield from self._select_row(
                            "dialog_style", "Dialog style", DIALOG_STYLE_CHOICES,
                            "Sidebar dialogs CARVE space out of the layout instead of "
                            "covering the chat, so you can still read the message that "
                            "provoked a tool call while you decide. They still BLOCK — "
                            "obscuring and blocking are separate things and only the "
                            "first changes. T075 spike: /test-sidebar honours this; the "
                            "existing dialogs do not yet.",
                        )
                        yield from self._select_row(
                            "dialog_side", "Sidebar side", DIALOG_SIDE_CHOICES,
                            "Which edge a sidebar dialog docks to. Only has an "
                            "effect while Dialog style is 'sidebar'. Separate "
                            "control on purpose: 'modal on the left' is not a "
                            "state, so one combined four-way control would have "
                            "to offer it or explain its absence.",
                        )
                        yield from self._switch_row(
                            "image_viewer_enabled",
                            "Auto-render pasted images",
                            "Paste an image (Ctrl+O) and it opens in the in-sidebar "
                            "viewer as real pixels. OFF still attaches the image to "
                            "the model — this only toggles the automatic preview.",
                        )
                        yield from self._switch_row(
                            "sidecar_enabled", "Prefer optional native sidecar",
                            "When installed, open the sidecar for settings and calendar; "
                            "Textual remains the fallback if it cannot launch. "
                            "The preview build is not connected yet.",
                        )
                        yield self._section_header("interface-footer")
                        yield Static("FOOTER", classes="set-subhead")
                        yield from self._switch_row(
                            "footer_show_seat", "Agent name",
                            "Also hides the red 'unregistered' warning — that field "
                            "reports a registration that did not happen.",
                        )
                        yield from self._switch_row(
                            "footer_show_thinking", "Thinking level", "e.g. think:medium",
                        )
                        # T579: these two shipped in T570 (0d099bc) as Settings
                        # fields with no control, and _collect REFUSES to save a
                        # partial object -- so EVERY save from this screen failed,
                        # not just these fields. The refusal was right; the screen
                        # was incomplete.
                        yield from self._switch_row(
                            "footer_show_bg", "Background processes",
                            "e.g. bg:2 -- absent when nothing is running.",
                        )
                        yield from self._switch_row(
                            "footer_show_subagents", "Subagents",
                            "e.g. agents:3 -- absent when none are running.",
                        )
                        yield from self._switch_row(
                            "footer_show_convo", "Conversation id",
                            "First 8 characters of the uuid.",
                        )
                        yield from self._switch_row(
                            "footer_show_context", "Context used / max",
                            "The raw token counts, e.g. ctx 23,133 / 120,064.",
                        )
                        yield from self._switch_row(
                            "footer_show_context_pct", "Context percent",
                            "How full the window is. Worth keeping on its own when the "
                            "raw counts are hidden — usually the only part read.",
                        )
                        yield from self._switch_row(
                            "footer_show_tps", "Tokens per second",
                            "Generation speed of the last turn.",
                        )
                        yield from self._text_row(
                            "footer_order", "Footer order (left to right)",
                            "Comma-separated ids. Use authority, plan, seat, think, "
                            "bg, agents, convo, ctx, pct, and tps. The switches "
                            "above control visibility; authority and plan stay "
                            "available because they are interactive status controls. "
                            "Unknown or repeated ids are ignored and missing ids "
                            "are appended in the default order.",
                            placeholder="authority, plan, seat, think, bg, agents, "
                            "convo, ctx, pct, tps",
                        )

            # One error line + one button row, OUTSIDE TabbedContent so both
            # stay visible whichever tab is active. A duplicate copy used to
            # live inside the last TabPane too — two widgets sharing
            # "#set-error" meant action_save()'s query_one() always found the
            # DOM-first one (buried in the Interface tab), so a save error
            # raised while on any OTHER tab wrote to a Static nobody could see.
            yield Static("", id="set-error")
            with Horizontal(id="set-buttons"):
                yield Button("Save", variant="primary", id="set-save")
                yield Button("Cancel", id="set-cancel")
                yield Button("Restore defaults", variant="warning", id="set-defaults")
                if self._settings_adapter.enabled:
                    yield Button("Retry failed", id="set-retry", disabled=True)
                # `.inline` — three buttons already share this row, and
                # SwapButton's own `width: 100%` would take all of it.
                yield SwapButton(classes="inline")

    # ── Actions ──────────────────────────────────────────────────────────────

    def _custom_theme_rows(self):
        """Name + all editable theme tokens, prefilled from the ACTIVE theme."""
        from litetui import themes as themes_mod
        try:
            resolved = self.app.current_theme.to_color_system().generate()
        except Exception:
            resolved = {}
        with Vertical(classes="set-row"):
            yield Label("New theme name", classes="set-label")
            yield Input(value="", id="ct-name", placeholder="my-theme")
        try:
            extras = dict(self.app.current_theme.variables or {})
        except Exception:
            extras = {}
        token_labels = {
            "footer-foreground": "Context footer text",
            "footer-background": "Context footer background",
        }
        for tok in themes_mod.THEME_FORM_TOKENS:
            with Vertical(classes="set-row"):
                yield Label(token_labels.get(tok, tok), classes="set-label")
                # The FIELD is the trigger: clicking it opens the picker
                # (see on_click). The first version put a "pick" Button beside
                # a 100%-width Input in a Horizontal — the button laid out
                # zero-wide past the right edge and shipped invisible, which
                # is this repo's dead-control class with a new costume.
                # The extras are theme VARIABLES, not Textual Theme fields, so
                # they are absent from the generated colour system -- prefill
                # them from the theme's own variables instead of leaving the
                # row blank, which would read as "unset" for a live colour.
                yield Input(value=str(resolved.get(tok) or extras.get(tok, "")),
                            id=f"ct-{tok}",
                            placeholder="#RRGGBB", classes="ct-hex")

    def on_click(self, event) -> None:
        """A click on any creator hex field opens the picker for that token.

        The field stays an Input so the value is still selectable/typable,
        but the CLICK is the picker's door — Ryan: "it should activate when
        i click any of those hex's". The picker's own hex box remains the
        typing escape hatch.
        """
        w = getattr(event, "widget", None)
        wid = getattr(w, "id", None) or ""
        if not isinstance(w, Input) or not wid.startswith("ct-") or wid == "ct-name":
            return
        event.stop()
        tok = wid[3:]
        box = w
        from litetui import themes as themes_mod
        try:
            resolved = self.app.current_theme.to_color_system().generate()
            merged = {**(self.app.current_theme.variables or {}), **resolved}
            presets = [(t, merged[t]) for t in themes_mod.THEME_FORM_TOKENS
                       if isinstance(merged.get(t), str)]
        except Exception:
            presets = []

        def _picked(hexv: str | None) -> None:
            if hexv:
                box.value = hexv

        # Both factories from ONE set of arguments -- `picker.pick`'s reason.
        initial = box.value.strip() or "#808080"
        present_dialog(
            self.app,
            partial(ColorPickerBody, initial, presets, tok),
            partial(ColorPickerScreen, initial, presets, tok),
            _picked,
        )

    def _collect(self) -> Settings:
        """Read every control into a new Settings, raising ValueError by name."""
        out = replace(self._start)
        typemap = {f.name: str(f.type) for f in fields(Settings)}
        missing: list[str] = []

        for f in fields(Settings):
            name = f.name
            if name in (
                "mcp_disabled_servers", "custom_themes", "plugins_disabled",
                # Per-model dicts, edited through /modelcfg — a flat text box
                # for a nested dict would be a control that corrupts on save.
                "llama_load_settings", "model_infer_overrides", "llama_presets",
                # Set by the first-boot picker, not by a visible control.
                "backend_chosen",
                # Legacy preference: playback is now explicitly controlled on
                # each response. There is deliberately no auto-speech checkbox.
                "tts_enabled",
            ):
                continue  # not one control; custom_themes is read from ct-*
            if settings_mod.source_of(name):
                continue  # env owns it; the control is disabled
            if name.startswith("ninfer_") and not gpu_gate.is_rtx_5090():
                continue  # T893: no NInfer tab on this box; the values ride through `replace()`
            try:
                widget = self.query_one(f"#f-{name}")
            except Exception:
                # NEVER `continue` here. A field whose widget is missing is a
                # field that cannot be saved, and skipping it silently is how a
                # save reports success while dropping half your settings — the
                # exact failure the dead-control audit found. If TabbedContent
                # ever mounts panes lazily, this is the line that catches it.
                missing.append(name)
                continue

            t = typemap[name]
            if isinstance(widget, Switch):
                setattr(out, name, bool(widget.value))
                continue
            if isinstance(widget, Select):
                val = widget.value
                # T640: was `if name == "default_model"`. Every optional-string
                # Select means UNSET by its blank option, not "the empty string"
                # — `default_model` was simply the only one until the two
                # Agent-loop pickers arrived. A per-field name check is wrong
                # the day somebody adds the fourth, and wrong SILENTLY: `""`
                # survives a truthiness read and fails an `is None` one.
                if "None" in t and "str" in t:
                    setattr(out, name, val or None)
                else:
                    setattr(out, name, val)
                continue

            raw = str(widget.value)
            try:
                if "list[str]" in t:
                    # stop, llama_attach_hosts, llama_models_dirs — comma-
                    # separated in one Input, the _text_row join reversed.
                    setattr(out, name, [s.strip() for s in raw.split(",") if s.strip()])
                elif "None" in t and "int" in t:
                    setattr(out, name, _num_or_none(raw, int))
                elif "None" in t and "float" in t:
                    setattr(out, name, _num_or_none(raw, float))
                elif "int" in t:
                    setattr(out, name, int(raw.strip()))
                elif "float" in t:
                    setattr(out, name, float(raw.strip()))
                elif "None" in t and "str" in t:
                    # An optional string: blank means UNSET, not the empty
                    # string. subagent_model is read as `settings.subagent_model
                    # or app.model_id`, so "" happens to work — but a later
                    # optional field read with `is None` would not, and the
                    # difference must not depend on the reader.
                    setattr(out, name, raw.strip() or None)
                else:
                    setattr(out, name, raw.strip())
            except ValueError:
                raise ValueError(f"{name}: {raw!r} is not a valid number")

        # Per-server MCP toggles
        disabled: list[str] = []
        for srv in self._mcp_servers:
            try:
                sw = self.query_one(f"#mcp-{srv}", Switch)
            except Exception:
                continue
            if not sw.value:
                disabled.append(srv)
        out.mcp_disabled_servers = disabled

        if missing:
            raise ValueError(
                "no control found for: "
                + ", ".join(missing)
                + " — refusing to save a partial settings object"
            )

        # Keep the persisted order complete even when a user hand-edits the
        # comma-separated field. Visibility is controlled by the switches, not
        # by accidentally leaving an id out of this layout preference.
        out.footer_order = settings_mod.normalize_footer_order(out.footer_order)

        # Range checks that would otherwise fail confusingly at request time.
        if not (1 <= out.autocompact_at_percent <= 99):
            raise ValueError("autocompact_at_percent: must be between 1 and 99")
        if out.tool_iterations < 1:
            raise ValueError("tool_iterations: must be at least 1")
        if out.custom_context_length < 0:
            raise ValueError('custom_context_length: must be zero (unknown) or positive')
        if out.custom_base_url:
            from litetui.custom_backend import api_base
            api_base(out.custom_base_url)
        if out.tool_policy_profile not in tool_policy.PROFILE_NAMES:
            raise ValueError(
                f"tool_policy_profile: must be one of {', '.join(tool_policy.PROFILE_NAMES)}"
            )
        if out.compact_max_tokens < 256:
            raise ValueError("compact_max_tokens: below 256 no summary can fit")
        if out.tool_context_threshold_chars < 0:
            raise ValueError("tool_context_threshold_chars: must be >= 0")
        if out.llama_models_max < 0:
            raise ValueError("llama_models_max: 0 means unlimited; below that is nothing")
        if out.lms_load_timeout_s < 30:
            raise ValueError("lms_load_timeout_s: under 30s no large model can load")
        if hasattr(out, "tts_timeout"):
            _validate_tts_timeout(out.tts_timeout)

        # The theme creator: a non-empty name mints (or overwrites) a custom
        # theme from the ct-* fields and SELECTS it, so Ctrl+S gives instant
        # feedback instead of a saved-but-invisible theme.
        from litetui import themes as themes_mod
        ct_name = self.query_one("#ct-name", Input).value.strip()
        if ct_name:
            # THE SAME LIST THE FORM RENDERS. _custom_theme_rows loops
            # THEME_FORM_TOKENS; this looped THEME_TOKENS, so the three extra
            # rows were drawn, accepted typing and opened the colour picker --
            # and were never read back. The theme saved without them and the
            # colours silently stayed default: a control that does nothing,
            # which is this repo's most-repeated defect.
            tokens = {
                tok: self.query_one(f"#ct-{tok}", Input).value.strip()
                for tok in themes_mod.THEME_FORM_TOKENS
            }
            themes_mod.theme_from_tokens(ct_name, tokens)  # raises, naming the field
            out.custom_themes = dict(out.custom_themes)
            out.custom_themes[ct_name] = tokens
            out.theme_name = ct_name
        return out

    # ── state carry across a live host swap ─────────────────────────────────

    def get_state(self) -> dict:
        """Every editable control, by id, walked from the DOM.

        🔴 THIS PANEL IS THIRTY-ODD CONTROLS AND A THEME CREATOR. A swap that
        rebuilt it from `self._start` would silently discard every edit made
        before the swap — and the rebuilt panel looks completely normal, just
        populated with the values you were changing away from.

        Derived from the DOM rather than from `fields(Settings)` because the
        panel also carries controls that are NOT settings fields: the theme
        creator's `ct-*` token inputs and `ct-name`, which `_collect` reads
        separately. A list keyed on the dataclass would drop exactly those.
        """
        out: dict = {}
        for w in list(self.query(Input)) + list(self.query(Select)) + list(self.query(Switch)):
            if w.id and not w.id.startswith("hook-"):
                out[w.id] = w.value
        out["_hooks_editor"] = self.query_one(HooksEditor).get_state()
        return out

    def set_state(self, state: dict) -> None:
        for wid, value in (state or {}).items():
            if wid == "_hooks_editor":
                self.query_one(HooksEditor).set_state(value)
                continue
            found = self.query(f"#{wid}")
            if not found:
                continue          # a control this host does not render
            try:
                found.first().value = value
            except Exception:
                continue          # a Select whose options no longer hold it

    def _dialog_default_target(self) -> tuple[Settings, tuple[str, ...]]:
        """Factory-reset only fields represented by this dialog.

        Environment-owned controls remain effective-only.  They are disabled
        in the form and must not be turned into writes merely because the
        factory value happens to match the effective value.
        """

        factory = Settings()
        target = replace(self._start)
        names: list[str] = []
        for field in fields(Settings):
            name = field.name
            if not self.query(f"#f-{name}") or settings_mod.source_of(name):
                continue
            names.append(name)
            setattr(target, name, deepcopy(getattr(factory, name)))
        return target, tuple(names)

    def _set_dialog_values(self, target: Settings, names: tuple[str, ...]) -> None:
        """Reflect a confirmed factory reset in mounted controls."""

        for name in names:
            found = self.query(f"#f-{name}")
            if not found:
                continue
            try:
                found.first().value = deepcopy(getattr(target, name))
            except (AttributeError, TypeError, ValueError):
                # A host-specific choice list may not expose a factory value;
                # persistence still receives the explicit target patch.
                continue

    def _show_error(self, raw: str, *, surface: str = "settings") -> None:
        mode = getattr(getattr(self.app, "settings", None), "error_message_style", "plain")
        self.query_one("#set-error", Static).update(present(raw, mode, surface=surface))

    def _show_save_result(self, result) -> None:
        messages: list[str] = []
        saved = [item.destination for item in result.persistence if item.saved]
        failed = persistence_error(result)
        if saved:
            messages.append(f"Saved: {', '.join(saved)}")
        if failed:
            messages.append(present(f"Save failed — {failed}", self.app.settings.error_message_style, surface="settings"))
        if result.has_pending_runtime:
            messages.append("Runtime changes are pending")
        if result.has_runtime_failures:
            runtime_errors = "; ".join(
                item.reason or f"{item.field}: runtime apply failed"
                for item in result.runtime
                if item.status == "failed"
            )
            messages.append(present(f"Runtime apply failed — {runtime_errors}", self.app.settings.error_message_style, surface="settings"))
        self.query_one("#set-error", Static).update(" · ".join(messages))
        retry = self.query("#set-retry")
        if retry:
            retry.first().disabled = not bool(self._settings_adapter.pending_changes)

    def _submit(self, new: Settings, *, force_fields: tuple[str, ...] = ()) -> None:
        self._draft.replace_working(new)
        try:
            result = self._settings_adapter.save(new, force_fields=force_fields)
        except Exception as exc:  # noqa: BLE001 - name the UI boundary failure
            self._show_error(f"Cannot save — {type(exc).__name__}: {exc}")
            return
        if result is None:
            # Legacy host contract: the app owns persistence and receives the
            # proposed Settings value through close_dialog.
            close_dialog(self, new)
            return
        self._last_save_result = result
        if (
            not result.persistence
            and not result.runtime
        ) or (
            result.fully_saved
            and not result.has_pending_runtime
            and not result.has_runtime_failures
            and not self._settings_adapter.pending_changes
        ):
            close_dialog(self, new)
            return
        self._show_save_result(result)

    def action_save(self) -> None:
        try:
            new = self._collect()
        except ValueError as e:
            self._show_error(f"Cannot save — {e}", surface="validation")
            field_name = str(e).split(":", 1)[0].strip()
            if field_name:
                try:
                    self.query_one(f"#f-{field_name}").focus()
                except Exception:
                    pass
            return
        self._submit(new)

    def action_retry(self) -> None:
        try:
            result = self._settings_adapter.retry()
        except SettingsConflictError as exc:
            self._show_error(f"Cannot retry — reload required for {', '.join(exc.destinations)}")
            return
        except Exception as exc:  # noqa: BLE001 - name the UI boundary failure
            self._show_error(f"Cannot retry — {type(exc).__name__}: {exc}")
            return
        self._last_save_result = result
        if result.fully_saved and not result.has_pending_runtime and not result.has_runtime_failures:
            close_dialog(self, self._draft.snapshot())
            return
        self._show_save_result(result)

    def action_cancel(self) -> None:
        self.request_cancel()

    @on(Button.Pressed, "#set-save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#set-cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#set-defaults")
    def _defaults(self) -> None:
        self.app.push_screen(
            SettingsExitConfirm(restore=True), self._on_restore_answer
        )

    def _on_restore_answer(self, answer: str | None) -> None:
        if answer == "restore":
            target, names = self._dialog_default_target()
            if not self._settings_adapter.enabled:
                # Preserve the historical host contract when no service is
                # bound: Restore returns a factory Settings object directly.
                close_dialog(self, Settings())
                return
            self._set_dialog_values(target, names)
            self._submit(target, force_fields=names)

    @on(Button.Pressed, "#set-retry")
    def _retry(self) -> None:
        self.action_retry()

    # ── Voice tab ─────────────────────────────────────────────────────────────
    #: True only between the Capture button and the next keypress, so on_key
    #: leaves every other key to the Inputs untouched.
    _capturing_hotkey = False

    @on(Button.Pressed, "#voice-test")
    def _voice_test(self) -> None:
        engine = self.query_one("#f-tts_engine", Select).value
        if engine == "edge":
            voice = self.query_one("#f-tts_edge_voice", Input).value
        else:
            voice = self.query_one("#f-tts_voice", Select).value
        timeout = getattr(self._start, "tts_timeout", 300)
        timeout_input = self.query("#f-tts_timeout")
        if timeout_input:
            try:
                timeout = _tts_timeout_from_input(
                    self.query_one("#f-tts_timeout", Input).value
                )
            except ValueError as exc:
                self.query_one("#voice-status", Static).update(str(exc))
                return
        ok = voice_backend.speak("This is the LiteTUI voice test.",
                                 engine=engine, voice=voice or None, timeout=timeout)
        self.query_one("#voice-status", Static).update(
            "Sent a test line — you should hear it now." if ok
            else "That engine is not installed — use Install, or pick pyttsx3.")

    @on(Button.Pressed, "#voice-capture")
    def _voice_capture(self) -> None:
        self._capturing_hotkey = True
        self.query_one("#voice-status", Static).update("Press the key combination…")

    def on_key(self, event) -> None:
        if not self._capturing_hotkey:
            return  # not capturing — every key reaches the Inputs as normal
        if event.key in ("ctrl", "shift", "alt", "meta", "super", "hyper"):
            return  # a bare modifier is not a hotkey; wait for the real key
        self.query_one("#f-stt_hotkey", Input).value = event.key
        self._capturing_hotkey = False
        self.query_one("#voice-status", Static).update(
            f"Record hotkey set to {event.key}. Save to apply.")
        event.stop()
        event.prevent_default()

    @on(Button.Pressed, "#voice-install-edge")
    def _voice_install_edge(self) -> None:
        self.query_one("#voice-status", Static).update(
            "Installing edge-tts + playsound…")
        self.run_worker(self._do_install_edge, thread=True)

    def _do_install_edge(self) -> None:
        from litetui.voice_install import install_edge

        msg = install_edge()
        self.app.call_from_thread(
            self.query_one("#voice-status", Static).update, msg)

    @on(Button.Pressed, "#voice-dl-stt")
    def _voice_dl_stt(self) -> None:
        size = self.query_one("#f-stt_model", Select).value
        self.query_one("#voice-status", Static).update(f"Downloading {size}…")
        self.run_worker(lambda: self._do_dl_stt(size), thread=True)

    def _do_dl_stt(self, size: str) -> None:
        import subprocess
        import sys

        try:
            if importlib.util.find_spec("faster_whisper") is None:
                subprocess.run([sys.executable, "-m", "pip", "install",
                                "faster-whisper"],
                               check=True, capture_output=True, timeout=600)
            from faster_whisper import WhisperModel
            WhisperModel(size, device="cpu", compute_type="int8")
            msg = f"{size} ready — the mic button and record hotkey now work."
        except Exception as e:  # noqa: BLE001 - report every failure to the panel
            msg = (f"download failed: {type(e).__name__} "
                   "(try: uv pip install faster-whisper)")
        self.app.call_from_thread(
            self.query_one("#voice-status", Static).update, msg)


class SettingsScreen(ModalScreen[Settings | None]):
    """The settings panel, as a modal. Returns the new Settings, or None.

    NOT replaced by `_ModalHost`: `app.py`'s centering rule names this class
    (`ConfirmStop, PickerScreen, HelpScreen, SettingsScreen, ...`) and several
    tests push it directly and assert on it. Bindings stay here as well as on
    the body — a ModalScreen is what has focus on the modal path — and their
    actions delegate down, so there is one implementation of each.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), *_SET_KEYS]

    def __init__(self, current: Settings, models: list[str] | None = None,
                 mcp_servers: list[str] | None = None,
                 loaded: list[str] | None = None, remote: bool = False,
                 *, snapshot_provider: SnapshotProvider | None = None,
                 save_patch: SavePatch | None = None,
                 runtime_apply: RuntimeApply | None = None):
        super().__init__()
        self._start = current
        self._models = models or []
        self._mcp_servers = mcp_servers or []
        self._loaded = loaded or []
        self._remote = remote
        self._snapshot_provider = snapshot_provider
        self._save_patch = save_patch
        self._runtime_apply = runtime_apply

    def compose(self) -> ComposeResult:
        yield SettingsBody(self._start, self._models, self._mcp_servers,
                           self._loaded, self._remote,
                           snapshot_provider=self._snapshot_provider,
                           save_patch=self._save_patch,
                           runtime_apply=self._runtime_apply)

    def action_save(self) -> None:
        self.query_one(SettingsBody).action_save()

    def action_cancel(self) -> None:
        self.query_one(SettingsBody).request_cancel()
