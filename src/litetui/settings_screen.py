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

from dataclasses import fields, replace
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
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

import settings as settings_mod
from colorpicker import ColorPickerScreen
from settings import Settings
from tool_policy import INTERACTIVE, PROFILE_NAMES, SCHEDULED

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
    import themes as themes_mod
    names = [n for n in BUILTIN_THEMES if n not in themes_mod.LIGHT_BUILTINS] + [
        n for n in themes_mod.ALL_THEMES if n not in BUILTIN_THEMES
    ] + [n for n in (custom or {}) if n not in themes_mod.ALL_THEMES]
    return [(n, n) for n in names]

TOOL_CONTEXT_CHOICES = [
    ("off — raw output enters context (baseline)", "off"),
    ("llm-tool-mask — pointer placeholder, no model call", "llm-tool-mask"),
    ("llm-tool-summ — side-call summary toward the task", "llm-tool-summ"),
]

TOOL_PROFILE_CHOICES = [
    ("interactive — inspect freely, confirm sensitive actions", INTERACTIVE),
    ("scheduled — read-only tools only", SCHEDULED),
]


class _Row(Horizontal):
    """One label + control + help line."""


def _num_or_none(raw: str, cast) -> Any:
    raw = raw.strip()
    if raw == "":
        return None
    return cast(raw)


class SettingsScreen(ModalScreen[Settings | None]):
    """Returns the new Settings on save, or None on cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=False),
    ]

    def __init__(self, current: Settings, models: list[str] | None = None,
                 mcp_servers: list[str] | None = None):
        super().__init__()
        self._start = current
        self._models = models or []
        self._mcp_servers = mcp_servers or []

    # ── Builders ─────────────────────────────────────────────────────────────

    def _text_row(self, name: str, label: str, help_text: str, placeholder: str = ""):
        locked = settings_mod.source_of(name)
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
                disabled=locked is not None,
                classes="set-input",
            )
            note = help_text
            if locked:
                note = f"LOCKED by ${locked} — unset it to edit here.  {help_text}"
            yield Static(note, classes="set-help")

    def _switch_row(self, name: str, label: str, help_text: str):
        with Vertical(classes="set-row"):
            with Horizontal(classes="set-switchline"):
                yield Switch(value=bool(getattr(self._start, name)), id=f"f-{name}")
                yield Label(label, classes="set-label-inline")
            yield Static(help_text, classes="set-help")

    def _select_row(self, name: str, label: str, choices, help_text: str):
        locked = settings_mod.source_of(name)
        with Vertical(classes="set-row"):
            yield Label(label, classes="set-label")
            yield Select(
                choices,
                value=getattr(self._start, name),
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
            # One tab per section. Each pane scrolls on its own, so no section
            # can push another off the bottom.
            with TabbedContent(id="set-tabs"):
                with TabPane("Model", id="tab-model"):
                    with VerticalScroll(classes="set-scroll"):

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

                        # ── Backend (engine selection) ───────────────────────
                        yield from self._select_row(
                            "backend", "Engine",
                            [("LM Studio desktop", "lmstudio"),
                             ("llama.cpp (our own llama-server)", "llamacpp")],
                            "Which engine serves the chat. /backend switches "
                            "live; this is the boot default.",
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
                with TabPane("Generation", id="tab-generation"):
                    with VerticalScroll(classes="set-scroll"):

                        yield from self._select_row(
                            "thinking_level", "Thinking level", THINKING_CHOICES,
                            "A level THIS SERVER accepts is not always one the LOADED model "
                            "accepts — a virtual model drops an unsupported value with a 200 "
                            "and reasons at its own default instead.",
                        )
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

                        yield from self._switch_row(
                            "tools_enabled", "Tools enabled",
                            "Off = plain chat, no bash/read/write/web_fetch.",
                        )
                        yield from self._text_row(
                            "tool_iterations", "Tool iterations per turn",
                            "The cap behind '[stopped — reached N tool iterations in one "
                            "turn]'. Raise it for long agent runs.",
                        )
                        yield from self._select_row(
                            "tool_policy_profile", "Conversation tool authority",
                            TOOL_PROFILE_CHOICES,
                            "Host-enforced. Interactive turns ask before writes, process "
                            "execution, desktop control, or other sensitive effects. "
                            "Scheduled is read-only and never opens an unattended prompt.",
                        )
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
                        yield from self._switch_row(
                            "enter_interrupts", "Enter interrupts mid-turn",
                            "OFF: Enter queues a mid-turn message; ctrl+shift+enter "
                            "interrupts. ON: the two swap — Enter interrupts, the chord "
                            "queues. Queued messages send when the turn ends; interrupt "
                            "keeps the partial reply and sends yours next.",
                        )

                        # ── Compaction ───────────────────────────────────────────────
                with TabPane("Compaction", id="tab-compaction"):
                    with VerticalScroll(classes="set-scroll"):

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
                        yield from self._switch_row(
                            "clear_screen_after_compact", "Clear screen after compacting",
                            "After compacting, the log still shows messages that were just "
                            "REPLACED — the screen and the real context disagree. Clearing "
                            "makes what you can scroll back to match what the model can see.",
                        )
                        yield from self._text_row(
                            "compact_max_tokens", "Compact max tokens",
                            "Budget for the summary itself. This was hardcoded at 2048 and is "
                            "why compaction returned no summary: reasoning consumed the whole "
                            "allowance before any summary was written.",
                        )
                        yield from self._select_row(
                            "compact_thinking_level", "Compact thinking level", THINKING_CHOICES,
                            "'off' is cheapest but is the value most likely to be silently "
                            "dropped by a virtual model, which then reasons at ITS default.",
                        )
                        yield from self._text_row(
                            "compact_max_tool_iters", "Compact tool rounds",
                            "How many tool round-trips compaction may take while persisting.",
                        )
                        yield from self._text_row(
                            "compact_keep_recent", "Keep recent messages",
                            "How many trailing messages survive verbatim after the summary.",
                        )

                        # ── Capabilities ─────────────────────────────────────────────
                with TabPane("Capabilities", id="tab-capabilities"):
                    with VerticalScroll(classes="set-scroll"):

                        yield from self._text_row(
                            "seat_name", "Fleet seat name",
                            "The name this seat asks the LiteHarness registry for, "
                            "and what the footer and `discover` show. Blank = LiteTUI.",
                            placeholder="LiteTUI",
                        )
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
                        yield from self._switch_row(
                            "mcp_enabled", "MCP servers",
                            "Start the servers declared in mcp.json.",
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
                                "  No mcp.json servers discovered.", classes="set-help"
                            )

                        # ── Interface ────────────────────────────────────────────────
                with TabPane("Themes", id="tab-themes"):
                    with VerticalScroll(classes="set-scroll"):
                        yield from self._select_row(
                            "theme_name", "Theme", _theme_choices(self._start.custom_themes),
                            "Dark built-ins, the LiteSuite ports (matrix, lite-suite, "
                            "amber-ledger...), the ten-gray SHADES, and your customs. "
                            "ctrl+p still has a quick-select; either way the pick "
                            "survives a restart.",
                        )
                        yield Static("CREATE / EDIT A CUSTOM THEME", classes="set-subhead")
                        yield Static(
                            "Fields are prefilled from the CURRENT theme, so start by "
                            "picking the nearest neighbour, then nudge. Name + Ctrl+S "
                            "creates it, selects it, and persists it. An existing "
                            "custom name is OVERWRITTEN - the creator is the editor. "
                            "Leave the name empty to save settings without creating.",
                            classes="set-help",
                        )
                        yield from self._custom_theme_rows()

                with TabPane("Interface", id="tab-interface"):
                    with VerticalScroll(classes="set-scroll"):

                        yield from self._switch_row(
                            "show_thinking", "Show thinking blocks",
                            "Render the model's reasoning trace in the transcript.",
                        )
                        yield from self._switch_row(
                            "autoscroll", "Follow output",
                            "Keep the log pinned to the newest message while streaming.",
                        )
                        yield Static("FOOTER", classes="set-subhead")
                        yield from self._switch_row(
                            "footer_show_seat", "Agent name",
                            "Also hides the red 'unregistered' warning — that field "
                            "reports a registration that did not happen.",
                        )
                        yield from self._switch_row(
                            "footer_show_thinking", "Thinking level", "e.g. think:medium",
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

    # ── Actions ──────────────────────────────────────────────────────────────

    def _custom_theme_rows(self):
        """Name + the 10 token fields, prefilled from the ACTIVE theme."""
        import themes as themes_mod
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
        for tok in themes_mod.THEME_FORM_TOKENS:
            with Vertical(classes="set-row"):
                yield Label(tok, classes="set-label")
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
        import themes as themes_mod
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

        self.app.push_screen(
            ColorPickerScreen(initial=box.value.strip() or "#808080",
                              presets=presets, token_name=tok),
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
            ):
                continue  # not one control; custom_themes is read from ct-*
            if settings_mod.source_of(name):
                continue  # env owns it; the control is disabled
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
                if name == "default_model":
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

        # Range checks that would otherwise fail confusingly at request time.
        if not (1 <= out.autocompact_at_percent <= 99):
            raise ValueError("autocompact_at_percent: must be between 1 and 99")
        if out.tool_iterations < 1:
            raise ValueError("tool_iterations: must be at least 1")
        if out.tool_policy_profile not in PROFILE_NAMES:
            raise ValueError(
                f"tool_policy_profile: must be one of {', '.join(PROFILE_NAMES)}"
            )
        if out.compact_max_tokens < 256:
            raise ValueError("compact_max_tokens: below 256 no summary can fit")
        if out.tool_context_threshold_chars < 0:
            raise ValueError("tool_context_threshold_chars: must be >= 0")
        if out.llama_models_max < 0:
            raise ValueError("llama_models_max: 0 means unlimited; below that is nothing")
        if out.lms_load_timeout_s < 30:
            raise ValueError("lms_load_timeout_s: under 30s no large model can load")

        # The theme creator: a non-empty name mints (or overwrites) a custom
        # theme from the ct-* fields and SELECTS it, so Ctrl+S gives instant
        # feedback instead of a saved-but-invisible theme.
        import themes as themes_mod
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

    def action_save(self) -> None:
        try:
            new = self._collect()
        except ValueError as e:
            self.query_one("#set-error", Static).update(f"[b]Cannot save[/b] — {e}")
            return
        self.dismiss(new)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#set-save")
    def _save(self) -> None:
        self.action_save()

    @on(Button.Pressed, "#set-cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#set-defaults")
    def _defaults(self) -> None:
        self.dismiss(Settings())
