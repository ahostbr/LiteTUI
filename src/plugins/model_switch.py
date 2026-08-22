"""Model selection, backend switching, and per-model configuration.

/model /models /reconnect — moved verbatim from the chain (self -> app).
/backend /load /unload /modelcfg — the dual-backend surface: two engines
(LM Studio desktop, our own llama-server) behind one seam (llm_backend).
The connection machinery (_connect, _fetch_ctx_window, _apply_context_length)
stays app-owned; these are its command surfaces.

ModelConfigScreen lives HERE because this plugin owns it (spec ruling 6:
screens move with their owner). It mirrors LM Studio's per-model panel —
Info / Load / Inference — for BOTH backends: on llama.cpp every field drives
a real llama-server flag (proven present in the installed build by the flag
census); on LM Studio only what the SDK can script is editable and the rest
says so instead of pretending. A control that lies is worse than one that is
visibly disabled — the settings screen's doctrine, inherited whole.
"""
from __future__ import annotations

import json
import shutil

from textual.binding import Binding
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Select, Static, TabbedContent, TabPane

import llm_backend
import paths  # noqa: F401 — path anchors come from ONE home (plugin rule)
import settings as settings_mod
from picker import PickerScreen
from plugins import PluginManifest


# ── /model /models /reconnect (bodies verbatim, rows now source-tagged) ──────

def _row_label(app, m: str) -> str:
    row = app.model_rows.get(m)
    tag = ""
    if row is not None:
        tag = f"  · {row.source}" + ("  · loaded" if row.loaded else "")
    return ("▸ " if m == app.model_id else "  ") + m + tag


def _cmd_model(app, name: str, arg: str) -> None:
    if arg:
        # Switch by number or name
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(app.available_models):
                app.model_id = app.available_models[idx]
                app._update_header()
                app._fetch_ctx_window()
                app._system(f"Switched to: {app.model_id}")
                # An explicit switch is an explicit act — the thing the
                # no-load-on-connect rule asks for. Boot still loads
                # nothing.
                app._apply_context_length()
            else:
                app._system(f"Invalid number. Use 1-{len(app.available_models)}")
        elif arg in app.available_models:
            app.model_id = arg
            app._update_header()
            app._fetch_ctx_window()
            app._system(f"Switched to: {app.model_id}")
            app._apply_context_length()
        else:
            app._system(f"Model not found: {arg}")
    elif not app.available_models:
        app._system("No models discovered — try /reconnect")
    else:
        # Clickable picker. `/model <n>` and `/model <name>` are handled
        # above and still work, so scripting and muscle memory survive.
        rows = [(m, _row_label(app, m)) for m in app.available_models]
        app.push_screen(
            PickerScreen("Select a model", rows, current=app.model_id),
            app._on_model_picked,
        )


def _cmd_reconnect(app, name: str, arg: str) -> None:
    app._connect()


# ── /backend ─────────────────────────────────────────────────────────────────

def _switch_backend(app, choice: str) -> None:
    if choice == app.backend.name:
        app._system(f"Already on {choice}")
        return
    s = app.settings
    s.backend = choice
    settings_mod.save(s)
    # Deliberately NOT shutting the old engine down: a mid-session flip that
    # evicted the resident model would make flipping back cost a full reload.
    # VRAM is freed explicitly (/unload) or at app exit (atexit).
    app.backend = llm_backend.make_backend(s)
    # The conversation is NOT touched — history survives an engine switch;
    # only the endpoint and the model list change.
    app.model_id = ""
    app._update_header()
    app._system(f"Backend → {choice}; reconnecting…")
    app._connect()


def _cmd_backend(app, name: str, arg: str) -> None:
    choice = arg.strip().lower()
    if choice in ("lmstudio", "llamacpp"):
        _switch_backend(app, choice)
        return
    if choice:
        app._system(f"Unknown backend {choice!r} — lmstudio or llamacpp")
        return
    lms_mark = "installed" if shutil.which("lms") else "not detected"
    llama_mark = (
        f"installed ({llm_backend.installed_build()})"
        if llm_backend.llama_available() else "not installed — see LiteSuite's Model Hub"
    )
    rows = [
        ("lmstudio", f"LM Studio desktop  · {lms_mark}"),
        ("llamacpp", f"llama.cpp (our engine)  · {llama_mark}"),
    ]

    def _picked(choice: str | None) -> None:
        if choice:
            _switch_backend(app, choice)

    app.push_screen(
        PickerScreen("Select the engine", rows, current=app.backend.name), _picked
    )


# ── /load /unload ────────────────────────────────────────────────────────────

def _cmd_load(app, name: str, arg: str) -> None:
    target = arg.strip() or app.model_id
    if not target:
        app._system("No model selected — /model first, or /load <name>")
        return

    async def _go() -> None:
        app._system(f"Loading {target}…")
        try:
            await app.backend.load(target)
        except llm_backend.BackendError as e:
            app._system(str(e))
            return
        app._system(f"Loaded: {target}")
        if target == app.model_id:
            app._fetch_ctx_window()
        else:
            app._connect()   # refresh the rows' loaded markers

    app.run_worker(_go(), group="modelctl", exclusive=True)


def _cmd_unload(app, name: str, arg: str) -> None:
    target = arg.strip() or app.model_id
    if not target:
        app._system("No model selected — /unload <name>")
        return

    async def _go() -> None:
        try:
            await app.backend.unload(target)
        except llm_backend.BackendError as e:
            app._system(str(e))
            return
        app._system(f"Unloaded: {target} — VRAM freed")
        if target == app.model_id:
            app._fetch_ctx_window()

    app.run_worker(_go(), group="modelctl", exclusive=True)


# ── /modelcfg — the per-model panel ─────────────────────────────────────────

#: Load tab rows: (cfg key, label, kind, extra). Kinds: int, float, text,
#: tri (server default / on / off), select (choices list). The cfg-key
#: vocabulary is llm_backend.FLAG_FOR — ONE table drives the screen, the
#: settings dict, and the ini generator.
_CACHE_TYPES = ["f16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0"]
_LOAD_FIELDS: list[tuple] = [
    ("ctx", "Context Length", "int", "model default"),
    ("ngl", "GPU Offload (layers)", "int", "99 = all layers"),
    ("threads", "CPU Thread Pool Size", "int", "server default"),
    ("batch", "Evaluation Batch Size", "int", "2048"),
    ("ubatch", "Physical Batch Size", "int", "512"),
    ("parallel", "Max Concurrent Predictions", "int", "1"),
    ("flash_attn", "Flash Attention", "select", ["auto", "on", "off"]),
    ("cache_k", "K Cache Quantization", "select", _CACHE_TYPES),
    ("cache_v", "V Cache Quantization", "select", _CACHE_TYPES),
    ("kv_offload", "Offload KV Cache to GPU", "tri", None),
    ("kv_unified", "Unified KV Cache", "tri", None),
    ("mlock", "Keep Model in Memory", "tri", None),
    ("mmap", "Try mmap()", "tri", None),
    ("context_shift", "Context Shift", "tri", None),
    ("seed", "Seed", "int", "random"),
    ("rope_base", "RoPE Frequency Base", "float", "auto"),
    ("rope_scale", "RoPE Frequency Scale", "float", "auto"),
    ("draft_model", "Speculative draft model (path)", "text", "off"),
    ("draft_max", "Max draft tokens", "int", "16"),
    ("draft_min", "Min draft tokens", "int", "0"),
    ("draft_p_min", "Draft probability", "float", "0.75"),
    ("mmproj", "Vision projector (mmproj path)", "text", "off"),
    ("chat_template_file", "Chat template file", "text", "model's own"),
]

#: Inference tab rows (override keys). Blank = inherit the global /settings
#: value; the placeholder SHOWS what is inherited so blank is never a mystery.
_INFER_FIELDS: list[tuple] = [
    ("temperature", "Temperature", "float"),
    ("top_p", "Top P Sampling", "float"),
    ("top_k", "Top K Sampling", "int"),
    ("min_p", "Min P Sampling", "float"),
    ("repeat_penalty", "Repeat Penalty", "float"),
    ("presence_penalty", "Presence Penalty", "float"),
    ("frequency_penalty", "Frequency Penalty", "float"),
    ("max_tokens", "Limit Response Length (tokens)", "int"),
    ("stop", "Stop Strings (comma-separated)", "text"),
]

#: LM Studio panel rows with NO llama-server equivalent — rendered as honest
#: statics, never as knobs that would accept input and change nothing.
_NA_ON_LLAMA = (
    ("Context Checkpoints", "n/a on llama.cpp — an LM Studio caching concept"),
    ("Reasoning Budget Message", "n/a in v1"),
    ("MTP speculative decoding", "n/a — llama.cpp speculates via a draft model (set one above)"),
    ("Context Overflow", "LiteTUI autocompacts instead — /settings → Compaction"),
    ("Preserve Thinking", "LiteTUI keeps reasoning out of resent history by design (context economy)"),
)


class ModelConfigScreen(ModalScreen[None]):
    """Info / Load / Inference for ONE model — LM Studio's panel, in the TUI.

    Unknown keys already present in the stored dicts are PRESERVED verbatim
    on save (forward compatibility: a newer LiteTUI's setting must survive a
    round-trip through an older screen, silent data loss being the one
    forbidden outcome).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "apply", "Apply", show=False),
    ]

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    # -- compose -----------------------------------------------------------

    def _load_cfg(self) -> dict:
        return dict(self.app.settings.llama_load_settings.get(self._key, {}))

    def _infer_cfg(self) -> dict:
        return dict(self.app.settings.model_infer_overrides.get(self._key, {}))

    def compose(self) -> ComposeResult:
        app = self.app
        on_llama = app.backend.name == "llamacpp"
        flags = llm_backend.installed_flags()
        load_cfg = self._load_cfg()
        infer_cfg = self._infer_cfg()
        row = app.model_rows.get(self._key)

        with Vertical(id="set-box"):
            engine = "llama.cpp" if on_llama else "LM Studio"
            yield Static(f"Model: {self._key}  ·  {engine}", id="set-title")
            yield Static(
                "Esc cancels · Ctrl+S applies (a loaded model reloads — "
                "evicts resident weights)",
                id="set-sub",
            )
            with TabbedContent(id="mc-tabs"):
                with TabPane("Info", id="mc-info"):
                    with VerticalScroll(classes="set-scroll"):
                        if row is not None:
                            yield Static(f"Source: {row.source}", classes="set-help")
                            if row.path:
                                yield Static(f"Path: {row.path}", classes="set-help")
                            for extra in row.extra_paths:
                                yield Static(f"Also at: {extra}", classes="set-help")
                            if row.modalities:
                                yield Static(
                                    f"Modalities: {', '.join(row.modalities)}",
                                    classes="set-help",
                                )
                            yield Static(
                                "Loaded" if row.loaded else "Not loaded (its ctx "
                                "number, if shown, is a ceiling — not a window)",
                                classes="set-help",
                            )
                        else:
                            yield Static("Not in the current model list.", classes="set-help")

                with TabPane("Load", id="mc-load"):
                    with VerticalScroll(classes="set-scroll"):
                        if not on_llama:
                            yield Static(
                                "LM Studio manages load settings in its own Load "
                                "panel — only Context Length is scriptable from "
                                "here (applied through the SDK).",
                                classes="set-help",
                            )
                        for key, label, kind, extra in _LOAD_FIELDS:
                            editable = on_llama or key == "ctx"
                            flag = llm_backend.FLAG_FOR.get(key, "")
                            missing = bool(flags) and flag not in flags
                            yield from self._field_rows(
                                "ld", key, label, kind, extra,
                                load_cfg.get(key),
                                disabled=(not editable) or missing,
                                note=(
                                    f"n/a in installed build ({llm_backend.installed_build()})"
                                    if missing else
                                    ("" if editable else "LM Studio manages this")
                                ),
                            )
                        for label, why in _NA_ON_LLAMA:
                            yield Static(f"{label}: {why}", classes="set-help")

                with TabPane("Inference", id="mc-infer"):
                    with VerticalScroll(classes="set-scroll"):
                        yield Static(
                            "Blank = inherit the global /settings value "
                            "(shown in the placeholder). These apply per "
                            "request, no reload needed.",
                            classes="set-help",
                        )
                        for key, label, kind in _INFER_FIELDS:
                            inherited = getattr(app.settings, key, None)
                            if key == "stop":
                                inherited = ",".join(app.settings.stop) or None
                            if key == "max_tokens":
                                inherited = (
                                    app.settings.max_tokens_tools
                                    if app.tools_enabled else app.settings.max_tokens_chat
                                )
                            yield from self._field_rows(
                                "inf", key, label, kind,
                                f"inherited: {inherited}" if inherited is not None else "server default",
                                infer_cfg.get(key),
                            )
                        if "jinja" in flags and on_llama:
                            yield from self._field_rows(
                                "inf", "enable_thinking", "Enable Thinking", "tri", None,
                                infer_cfg.get("enable_thinking"),
                                note="via chat_template_kwargs — model must support it",
                            )
                        yield Label("Structured Output (JSON schema)", classes="set-label")
                        # An Input, not a TextArea: the TextArea would not take
                        # focus from tab or click in the live walk (schema entry
                        # was simply impossible), and a schema is one line of
                        # JSON anyway. Revisit if multiline editing is ever real.
                        yield Input(
                            value=infer_cfg.get("json_schema") or "",
                            placeholder="paste or type a JSON schema — empty = off",
                            id="mc-json-schema",
                            classes="set-input",
                        )
                        yield Static(
                            "Empty = off. Invalid JSON is refused at apply.",
                            classes="set-help",
                        )
                        yield Label("Presets", classes="set-label")
                        preset_names = sorted(app.settings.llama_presets)
                        yield Select(
                            [("(apply a saved preset…)", "")] + [(n, n) for n in preset_names],
                            value="",
                            id="mc-preset-apply",
                            allow_blank=False,
                        )
                        yield Input(
                            placeholder="save current Load+Inference as preset — type a name, Ctrl+S",
                            id="mc-preset-name",
                        )

    def _field_rows(self, prefix: str, key: str, label: str, kind: str, extra,
                    current, disabled: bool = False, note: str = ""):
        wid = f"{prefix}-{key}"
        with Vertical(classes="set-row"):
            yield Label(label, classes="set-label")
            if kind == "tri":
                choices = [("server default", ""), ("on", "true"), ("off", "false")]
                value = "" if current is None else ("true" if current else "false")
                yield Select(choices, value=value, id=wid,
                             allow_blank=False, disabled=disabled)
            elif kind == "select":
                choices = [("server default", "")] + [(c, c) for c in extra]
                value = current if current in (extra or []) else ""
                yield Select(choices, value=value, id=wid,
                             allow_blank=False, disabled=disabled)
            else:
                shown = "" if current is None else str(current)
                placeholder = extra if isinstance(extra, str) else ""
                yield Input(value=shown, placeholder=placeholder or "unset",
                            id=wid, disabled=disabled, classes="set-input")
            if note:
                yield Static(note, classes="set-help")

    # -- collect / apply ---------------------------------------------------

    def _collect_group(self, prefix: str, fields: list[tuple], base: dict) -> dict:
        """Read widgets back into a cfg dict. Keys the screen does not render
        (unknown/newer) survive from `base` untouched."""
        out = dict(base)
        for spec in fields:
            key, _label, kind = spec[0], spec[1], spec[2]
            wid = f"{prefix}-{key}"
            try:
                widget = self.query_one(f"#{wid}")
            except Exception:
                continue   # disabled-by-census fields may be absent; keep base value
            if isinstance(widget, Select):
                raw = widget.value or ""
                if raw == "":
                    out.pop(key, None)
                elif kind == "tri":
                    out[key] = raw == "true"
                else:
                    out[key] = raw
                continue
            raw = str(widget.value).strip()
            if not raw:
                out.pop(key, None)
                continue
            try:
                if kind == "int":
                    out[key] = int(raw)
                elif kind == "float":
                    out[key] = float(raw)
                elif key == "stop":
                    out[key] = [s.strip() for s in raw.split(",") if s.strip()]
                else:
                    out[key] = raw
            except ValueError:
                raise ValueError(f"{key}: {raw!r} is not a valid number")
        return out

    def action_apply(self) -> None:
        app = self.app
        s = app.settings
        try:
            load_cfg = self._collect_group("ld", _LOAD_FIELDS, self._load_cfg())
            infer_fields = _INFER_FIELDS + [("enable_thinking", "", "tri")]
            infer_cfg = self._collect_group("inf", infer_fields, self._infer_cfg())
            schema_text = str(self.query_one("#mc-json-schema", Input).value).strip()
            if schema_text:
                json.loads(schema_text)   # refuse invalid JSON HERE, by name
                infer_cfg["json_schema"] = schema_text
            else:
                infer_cfg.pop("json_schema", None)
        except ValueError as e:
            app._system(f"Not applied — {e}")
            return

        # Preset save (the theme-creator idiom: a non-empty name mints one).
        preset_name = self.query_one("#mc-preset-name", Input).value.strip()
        if preset_name:
            s.llama_presets = dict(s.llama_presets)
            s.llama_presets[preset_name] = {"load": dict(load_cfg), "inference": dict(infer_cfg)}
        applied = self.query_one("#mc-preset-apply", Select).value or ""
        if applied:
            preset = s.llama_presets.get(applied, {})
            load_cfg = {**load_cfg, **preset.get("load", {})}
            infer_cfg = {**infer_cfg, **preset.get("inference", {})}

        prior_load = s.llama_load_settings.get(self._key, {})
        s.llama_load_settings = dict(s.llama_load_settings)
        if load_cfg:
            s.llama_load_settings[self._key] = load_cfg
        else:
            s.llama_load_settings.pop(self._key, None)
        s.model_infer_overrides = dict(s.model_infer_overrides)
        if infer_cfg:
            s.model_infer_overrides[self._key] = infer_cfg
        else:
            s.model_infer_overrides.pop(self._key, None)
        settings_mod.save(s)

        key = self._key
        if app.backend.name == "llamacpp" and load_cfg != prior_load:
            async def _apply() -> None:
                app._system(f"Applying load settings to {key} (reload)…")
                try:
                    await app.backend.apply_load_settings(key, load_cfg)
                except llm_backend.BackendError as e:
                    app._system(str(e))
                    return
                app._system(f"{key}: load settings applied")
                if key == app.model_id:
                    app._fetch_ctx_window()
            app.run_worker(_apply(), group="modelctl", exclusive=True)
        elif app.backend.name == "lmstudio" and load_cfg.get("ctx") != prior_load.get("ctx"):
            async def _apply_lms() -> None:
                try:
                    await app.backend.apply_load_settings(key, {"ctx": load_cfg.get("ctx")})
                except llm_backend.BackendError as e:
                    app._system(str(e))
                    return
                app._system(f"{key}: context length applied")
                if key == app.model_id:
                    app._fetch_ctx_window()
            app.run_worker(_apply_lms(), group="modelctl", exclusive=True)

        app._system(f"Saved model config for {key}")
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _cmd_modelcfg(app, name: str, arg: str) -> None:
    target = arg.strip() or app.model_id
    if not target:
        app._system("No model selected — /model first, or /modelcfg <name>")
        return
    app.push_screen(ModelConfigScreen(target))


# ── First-boot engine picker ─────────────────────────────────────────────────

def _activate(app) -> None:
    """Exactly once, and only when BOTH engines are detected, ask which one
    this seat should use. One engine → chosen silently. Esc → not marked
    chosen, so the question returns next boot (a dismissed question is not an
    answer). Detection is deliberately offline-cheap: an exe on disk and a
    CLI on PATH — no network probes on the boot path."""
    s = app.settings
    if s.backend_chosen:
        return
    if settings_mod.source_of("backend"):
        return   # the environment already chose; a question would be a lie
    lms_present = bool(shutil.which("lms"))
    llama_present = llm_backend.llama_available()
    if not lms_present and not llama_present:
        return   # nothing to choose between; ask when one appears
    if lms_present != llama_present:
        s.backend = "llamacpp" if llama_present else "lmstudio"
        s.backend_chosen = True
        settings_mod.save(s)
        if s.backend != app.backend.name:
            app.backend = llm_backend.make_backend(s)
            app._connect()
        return

    rows = [
        ("lmstudio", "LM Studio desktop — the lms.exe you already run"),
        ("llamacpp", f"llama.cpp — our own engine ({llm_backend.installed_build()})"),
    ]

    def _picked(choice: str | None) -> None:
        if choice is None:
            return   # ask again next boot
        s.backend = choice
        s.backend_chosen = True
        settings_mod.save(s)
        if choice != app.backend.name:
            app.backend = llm_backend.make_backend(s)
            app.model_id = ""
        app._update_header()
        app._connect()

    app.push_screen(
        PickerScreen("Which engine should serve this seat?", rows, current=s.backend),
        _picked,
    )


# ── Registration ─────────────────────────────────────────────────────────────

def _register(ctx) -> None:
    ctx.command(
        ("/model", "/models"), _cmd_model,
        palette="Switch model",
        help="Pick from the connected server's models (/model)",
    )
    ctx.command(
        ("/reconnect",), _cmd_reconnect,
        palette="Reconnect",
        help="Reconnect to the model server (/reconnect)",
    )
    ctx.command(
        ("/backend",), _cmd_backend,
        palette="Switch backend",
        help="lmstudio or llamacpp — which engine serves the chat (/backend)",
    )
    ctx.command(
        ("/load",), _cmd_load,
        palette="Load model",
        help="Explicitly load a model into the engine (/load [name])",
    )
    ctx.command(
        ("/unload",), _cmd_unload,
        palette="Unload model",
        help="Unload a model and free its VRAM (/unload [name])",
    )
    ctx.command(
        ("/modelcfg",), _cmd_modelcfg,
        palette="Model config",
        help="Per-model Load/Inference panel — LM Studio parity (/modelcfg [name])",
    )


PLUGIN = PluginManifest(id="model-switch", register=_register, activate=_activate)
