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

import difflib
import json
import shutil
from functools import partial
from pathlib import Path

from textual.binding import Binding
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, Select, Static, Switch, TabbedContent, TabPane

from litetui import settings_runtime
from litetui import llm_backend
from litetui import model_transport
from litetui import paths  # noqa: F401 — path anchors come from ONE home (plugin rule)
from litetui import settings as settings_mod
from litetui.picker import pick
from litetui.plugins import PluginManifest
from litetui.side_panel import SwapButton, close_dialog, present_dialog


# ── /model /models /reconnect (bodies verbatim, rows now source-tagged) ──────

def _row_label(app, m: str) -> str:
    row = app.model_rows.get(m)
    tag = ""
    if row is not None:
        status = "ready" if getattr(getattr(app, "backend", None), "remote", False) else "loaded"
        tag = f"  · {row.source}" + (f"  · {status}" if row.loaded else "")
    return ("▸ " if m == app.model_id else "  ") + m + tag


def switch_model(app, target: str) -> bool:
    """Switch through the same effects as ``/model`` and the RPC host.

    A host-side picker is another entrance to LiteTUI's existing control, not a
    parallel assignment: header/context/thinking refresh and the explicit local
    context apply must remain identical whichever surface chose the model.
    """
    if target not in app.available_models:
        return False
    settings_runtime.save_selection_defaults(backend=app.backend.name, default_model=target)
    if target == app.model_id:
        return True
    app.model_id = target
    app._model_thinking_levels = None
    from litetui import thinking_probe

    thinking_probe.clear_cache(target)
    app.update_header()
    app.fetch_context_window()
    app.system_message(f"Switched to: {app.model_id}")
    # An explicit switch is an explicit act — the thing the no-load-on-connect
    # rule asks for. Boot still loads nothing.
    app.apply_context_length()
    if app.backend.name == "lmstudio":
        app._probe_thinking()
    app._rpc_emit_model_state()
    return True


def _empty_state_hint(app) -> str:
    """What THIS backend wants said when it has no models to offer.

    🔴 THE SITES BELOW USED TO HARDCODE "/reconnect" AND "/model first" (T860).
    On NInfer with no engine every one of those is a remedy the broken state has
    removed: /reconnect reconnects to nothing, /model needs a discovered list
    that cannot exist, /load needs a model the engine defines.

        T858 WAS A HANDLER THAT *NEEDED* THE MISSING THING. THIS IS A HANDLER
        THAT *RECOMMENDS* IT. One root — the message was written for the state
        where the backend works.

    Asked of the backend by `getattr`, not added to a base class: a backend that
    says nothing keeps today's words exactly, which is what stops this being a
    global find-and-replace.
    """
    return backend_hint(app.backend)


# 🔴 T889. The BODY of this helper moved to `litetui.llm_backend.backend_hint`:
# app.py must reach it without importing a plugin module (test_plugin_dogfood
# gates exactly that), and the backend layer is the home it belongs in. This
# alias keeps `model_switch.backend_hint` resolvable — so `_empty_state_hint`
# below stays verbatim and any caller still naming it here is unaffected.
backend_hint = llm_backend.backend_hint


def _ninfer_artifact_rows(app) -> list[tuple[str, str]]:
    from litetui.ninfer_engine import list_ninfer_artifacts

    chosen = str(getattr(app.settings, "ninfer_artifact", "") or "").strip()
    rows = []
    for path in list_ninfer_artifacts():
        mark = "  · current" if chosen and Path(chosen) == path else ""
        size = ""
        try:
            size = f"  · {path.stat().st_size / 1_000_000_000:.1f} GB"
        except OSError:
            pass
        rows.append((str(path), f"{path.stem}{size}{mark}"))
    return rows


def _pick_ninfer_artifact(app) -> None:
    """`/model` on NInfer: choose the FILE the next engine will serve.

    ⬜ THE MODEL IS THE ARTIFACT HERE, and that is not a wording difference. One
    artifact per process, chosen before the engine starts, so there is no server
    list to show and no load to perform. Ryan had three `.ninfer` files on disk,
    `ninfer_artifact()` refuses to guess between them (rightly), and no command
    in the TUI could make the choice — the only door was typing a path into
    /settings. This is that door, on the surface he already reaches for.
    """
    rows = _ninfer_artifact_rows(app)
    if not rows:
        from litetui.ninfer_engine import artifacts_dir
        app.system_message(
            f"No .ninfer artifacts in {artifacts_dir()} — LiteSuite's Model Hub "
            "pulls them, or set ninfer_artifact in /settings."
        )
        return

    def _picked(choice: str | None) -> None:
        if not choice:
            return
        import dataclasses
        app._on_settings_saved(
            dataclasses.replace(app.settings, ninfer_artifact=choice))
        result = getattr(app, '_settings_save_result', None)
        if result is not None and any(not p.saved for p in result.persistence):
            return
        settings_runtime.save_selection_defaults(backend="ninfer", ninfer_artifact=choice)
        app.system_message(
            f"NInfer artifact set to {Path(choice).stem} — /engine start to serve it."
        )

    pick(app, "Select a NInfer artifact (served on the next /engine start)",
         rows, _picked,
         current=str(getattr(app.settings, "ninfer_artifact", "") or ""))


def _cmd_model(app, name: str, arg: str) -> None:
    if not arg and getattr(app.backend, "name", "") == "ninfer":
        _pick_ninfer_artifact(app)
        return
    if arg:
        # Switch by number or name
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(app.available_models):
                switch_model(app, app.available_models[idx])
            else:
                app.system_message(f"Invalid number. Use 1-{len(app.available_models)}")
        elif not switch_model(app, arg):
            app.system_message(f"Model not found: {arg}")
    elif not app.available_models:
        app.system_message(f"No models discovered — {_empty_state_hint(app)}")
    else:
        # Clickable picker. `/model <n>` and `/model <name>` are handled
        # above and still work, so scripting and muscle memory survive.
        rows = [(m, _row_label(app, m)) for m in app.available_models]
        pick(app, "Select a model", rows, app.on_model_picked, current=app.model_id)


def _cmd_reconnect(app, name: str, arg: str) -> None:
    if getattr(app, '_chat_running', lambda: False)():
        app.system_message('Finish or stop the current turn before reconnecting.')
        return
    settings_runtime.prepare_reconnect(app)
    app.connect()


# ── /backend ─────────────────────────────────────────────────────────────────

def _switch_backend(app, choice: str) -> None:
    if getattr(app, "_chat_running", lambda: False)():
        app.system_message("Finish or stop the current turn before switching backends.")
        return
    if choice == app.backend.name:
        settings_runtime.save_selection_defaults(backend=choice, backend_chosen=True)
        app.system_message(f"Already on {choice}")
        return
    # The SEQUENCE moved to `App.apply_backend_change` so /settings could reach
    # it too — saving a changed `backend` used to write settings.json and stop
    # there, leaving the app on the old engine (Ryan, 2026-09-18). It could not
    # live here and be called from app.py: app.py may not import a plugin
    # module, which `test_app_never_imports_a_plugin_module` enforces.
    # The guards above stay here, where the user typed the command.
    app.apply_backend_change(choice)
    if app.backend.name == choice:
        settings_runtime.save_selection_defaults(backend=choice, backend_chosen=True)


def _ninfer_mark(app) -> str:
    from litetui import ninfer_engine
    from litetui.ninfer_backend import discover_ninfer_host
    reg = str(getattr(app.settings, "ninfer_host", "") or "").strip() or discover_ninfer_host()
    if reg:
        return f"engine registered at {reg}"
    return ("installed — /engine start" if ninfer_engine.ninfer_executable(app.settings).is_file()
            else "not installed — LiteSuite's Model Hub installs it")


def _set_lanes(app, raw: str) -> str:
    """`/engine lanes N` — the --max-concurrency of the NEXT start (T892).

    RYAN, 2026-09-18 13:4x (liteask a-f30ad840): *"during the slash engine cmd in
    litetui ... we need to be able to set this"*. Saved to settings, the same field
    the NInfer tab edits; a running engine keeps its lanes until restarted, and the
    reply says which it has. Out of range is refused with the range, not clamped:
    the user typed a number and should learn the legal ones.
    """
    from litetui import ninfer_engine

    lo, hi = ninfer_engine.NINFER_CONCURRENCY_RANGE
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        return (f"/engine lanes N — N is {lo}..{hi}: requests the engine decodes together "
                "(its --max-concurrency). They share the --max-context KV pool.")
    s = app.settings
    s.ninfer_max_concurrency = int(raw)
    settings_runtime.persist_or_raise(app, s)
    running = getattr(app.backend, "engine_concurrency", lambda: None)()
    tail = f"; the running engine has {running}" if running else ""
    return (f"NInfer lanes set to {raw} — applies on the next /engine start{tail}. "
            "Lanes share the --max-context KV pool: no extra VRAM to speak of, less context each under load.")


def _cmd_engine(app, name: str, arg: str) -> None:
    """/engine start [N]|stop|status|lanes N — the NInfer process LiteTUI may own."""
    backend = app.backend
    if getattr(backend, "name", "") != "ninfer":
        app.system_message("/engine drives the NInfer backend — /backend ninfer first.")
        return
    verb, *rest = (arg.strip().lower() or "status").split()
    if verb == "status":
        app.system_message(backend.engine_status())
        return
    if verb == "stop":
        app.system_message(backend.stop_engine())
        return
    if verb in ("lanes", "concurrency"):
        app.system_message(_set_lanes(app, rest[0] if rest else ""))
        return
    if verb != "start":
        app.system_message("/engine start [lanes] | stop | status | lanes N")
        return
    if rest:
        # `/engine start 4` — set the lanes, then start with them.
        said = _set_lanes(app, rest[0])
        app.system_message(said)
        if said.startswith("/engine"):
            return
    if getattr(app, "_chat_running", lambda: False)():
        app.system_message("Finish or stop the current turn before starting the engine.")
        return
    # 🔴 T865. This line used to be printed HERE, before the await. Five
    # conditions refuse inside start() — an engine already registered, one still
    # loading, a full card, no exe, no artifact — so on every one of them the
    # user read a promise and then its contradiction:
    #
    #     Starting ninfer-serve — weights take about ten seconds…
    #     ninfer-serve not installed at C:\… — install it from…
    #
    # Both sentences are correct alone and the end state is right, which is why
    # no test saw it; what was wrong is the ORDER A PERSON READS THEM IN, and
    # only Sentinel driving the exe-missing arm found it. The launcher calls this
    # back immediately before the spawn, from its worker thread.
    def _starting() -> None:
        app.call_from_thread(
            app.system_message,
            "Starting ninfer-serve — weights take about ten seconds…")

    async def _go():
        try:
            msg = await backend.start_engine(notice=_starting)
        except llm_backend.BackendError as e:
            app.system_message(str(e))
            return
        app.system_message(msg)
        app.connect()

    app.run_worker(_go(), exclusive=False, name="ninfer-engine-start")


def _cmd_backend(app, name: str, arg: str) -> None:
    choice = arg.strip().lower()
    if choice in llm_backend.BACKEND_NAMES or choice in model_transport.OAUTH_PROVIDERS:
        _switch_backend(app, choice)
        return
    if choice:
        app.system_message(f"Unknown backend {choice!r} — {', '.join(llm_backend.BACKEND_NAMES)}")
        return
    lms_mark = "installed" if shutil.which("lms") else "not detected"
    llama_mark = (
        f"installed ({llm_backend.configured_build(app.settings)})"
        if llm_backend.llama_available(app.settings) else "not installed — choose an installed executable in Settings"
    )
    rows = [
        ("custom", f"Custom server  · {app.settings.custom_base_url or 'set URL in /settings'}"),
        ("lmstudio", f"LM Studio desktop  · {lms_mark}"),
        ("llamacpp", f"llama.cpp (our engine)  · {llama_mark}"),
        ("ninfer", f"NInfer (5090 engine)  · {_ninfer_mark(app)}"),
        ("codex", f"Codex subscription  · {model_transport.auth_status('codex')}"),
    ]
    from litetui import gpu_gate

    if not gpu_gate.is_rtx_5090():
        rows = [r for r in rows if r[0] != "ninfer"]   # T893: not a 5090 -> no such row

    def _picked(choice: str | None) -> None:
        if choice:
            _switch_backend(app, choice)

    pick(app, "Select the engine", rows, _picked, current=app.backend.name)


# ── /load /unload ────────────────────────────────────────────────────────────

def _row_for(app, key: str):
    """The discovered row for `key`, or None. Errors are not this caller's."""
    try:
        for row in llm_backend.scan_models(app.settings):
            if row.key == key:
                return row
    except Exception:
        pass
    return None


def _say_projector(app, key: str) -> None:
    """Name the projector a load resolved — or say why it resolved none.

    🔴 ANNOUNCED, NOT SILENT (Ryan's ruling, 2026-09-03, relayed by Sentinel).
    A guess that changes a model's MODALITIES must not be invisible: with a
    projector the model can read images, and without one it answers as if the
    image were not there. Both outcomes look identical at the prompt, which is
    exactly why the quiet version of this feature would be worse than none.

    Says nothing when the user set the path themselves — that is not a guess,
    and a line per load about a setting they typed is noise.
    """
    row = _row_for(app, key)
    if row is None or row.path is None:
        return
    cfg = (app.settings.llama_load_settings or {}).get(key, {})
    if llm_backend.is_no_projector(cfg.get("mmproj")):
        # ⬜ THE ONE EXPLICIT SETTING THAT STILL SPEAKS. Everything else the
        # user typed is their own and a line per load would be noise — but
        # "this vision model is loading TEXT-ONLY" is the same modality change
        # the auto-pair announcement exists for, seen from the other side. If
        # the guess must not be silent, neither must the refusal of it.
        app.system_message("vision: none (your choice)")
        return
    if "mmproj" in cfg:
        return                      # explicit: their choice, not our guess
    auto, found = llm_backend.sibling_mmproj(row.path)
    if auto:
        app.system_message(f"vision: paired {Path(auto).name} (found beside the model)")
    elif len(found) > 1:
        # ⚠️ THE COUNT IS THE REASON, so it is IN the line. "No projector" alone
        # sends someone to look for a missing file; this sends them to the one
        # decision that resolves it.
        app.system_message(
            f"vision: {len(found)} projectors beside this model — none paired, "
            f"text only. Set one in /modelcfg → Load → Vision projector: "
            + ", ".join(Path(f).name for f in found)
        )


def _notice(app, text: str):
    """A callback the backend fires from its worker thread once the work is
    certain (T873). `call_from_thread` is required: every backend announces from
    inside `asyncio.to_thread`, and Textual refuses a cross-thread write."""
    return lambda: app.call_from_thread(app.system_message, text)


#: Sentinel for `_start_load(ctx=...)`: the caller chose no context, so fall back
#: to the saved `default_context_length`. Distinct from an explicit ``None`` — the
#: /load picker's toggle OFF means "load at the server's own default".
_USE_DEFAULT = object()


def _start_load(app, target: str, ctx=_USE_DEFAULT) -> None:
    """Load `target`. ONE body for the typed name and the picked one.

    ``ctx``: ``_USE_DEFAULT`` uses the saved ``default_context_length``; an int or
    ``None`` (from the /load picker's context row) overrides it for this load.
    """

    async def _go() -> None:
        # 🔴 T873. "Loading {target}…" was printed HERE, before the await, and
        # llamacpp's `_load_sync` opens with `_refuse_if_attached`. On a server
        # LiteTUI adopted or one started with a single -m <gguf>, the user read
        # a promise and then its contradiction — T865's shape, one command over.
        #
        # The picker load must honour the configured context length, exactly as
        # the /model path does (set_active_model -> apply_context_length). Without
        # ctx=, LM Studio loads at the model's own default (8192) and the saved
        # "Load with context length" is silently ignored — the picker was the one
        # load path that skipped the apply the /model path has.
        want = app.settings.default_context_length if ctx is _USE_DEFAULT else ctx
        _msg = (f"Loading {target} at {want:,} tokens…" if want
                else f"Loading {target}…")
        try:
            await app.backend.load(target, ctx=want, notice=_notice(app, _msg))
        except llm_backend.BackendError as e:
            app.system_message(str(e))
            return
        app.system_message(f"Loaded: {target}")
        _say_projector(app, target)
        # The model we just loaded becomes the active one, and the footer follows
        # it. On LM Studio (one resident model) this load EVICTS whatever was
        # active, so leaving the old model_id in place would point the
        # conversation — and the footer's context window — at a model no longer in
        # VRAM, which then reads its CEILING (262,144) instead of the window we
        # just loaded. Adopt the target and refresh: set_active_model's tail minus
        # the load we already did (no second, ctx-less load).
        from litetui import thinking_probe
        app.model_id = target
        app._model_thinking_levels = None
        thinking_probe.clear_cache(target)
        app.update_header()
        app.fetch_context_window()
        if getattr(app.backend, "name", "") == "lmstudio":
            app._probe_thinking()
        app._rpc_emit_model_state()

    app.run_worker(_go(), group="modelctl", exclusive=True)


def _load_rows(app, first: list[str] | None = None) -> list[tuple[str, str]]:
    """The same rows `/model` shows, optionally with some floated to the top.

    `_row_label` already carries the loaded marker, so the picker and `/model`
    cannot disagree about which model is resident — that label is the one place
    it is decided.
    """
    order = list(app.available_models)
    for m in reversed(first or []):
        if m in order:
            order.remove(m)
            order.insert(0, m)
    return [(m, _row_label(app, m)) for m in order]


def _on_load_picked(app, model_id: str | None) -> None:
    # The context row (if the picker showed one) stashed its choice on the app at
    # selection; read and clear it whichever way this resolves.
    ctx = getattr(app, "_load_ctx_override", _USE_DEFAULT)
    app._load_ctx_override = _USE_DEFAULT
    # Esc resolves with None and the callback still fires — see picker.pick.
    if model_id is None:
        return
    _start_load(app, model_id, ctx=ctx)


def _load_ctx_row(app):
    """The /load picker's context-length row: a toggle + a token count, pre-filled
    from the saved default. Built HERE so the generic picker imports no load
    widgets; `_read_load_ctx` reads it back at selection time."""
    saved = app.settings.default_context_length
    yield Horizontal(
        Switch(value=bool(saved), id="load-ctx-on"),
        Label("Load with context length", id="load-ctx-label"),
        Input(value=(str(saved) if saved else ""), placeholder="e.g. 131072",
              id="load-ctx-val"),
        id="load-ctx-row",
    )


def _read_load_ctx(app, body) -> None:
    """Stash the context row's choice on the app for `_on_load_picked`.
    Toggle OFF -> None (load at the server's own default); ON + a number -> that
    many tokens; ON + blank/garbage -> None (nothing to apply)."""
    try:
        on = body.query_one("#load-ctx-on", Switch).value
        raw = body.query_one("#load-ctx-val", Input).value.strip().replace(",", "")
    except Exception:
        app._load_ctx_override = _USE_DEFAULT
        return
    if not on or not raw:
        app._load_ctx_override = None
        return
    try:
        app._load_ctx_override = int(raw)
    except ValueError:
        app._load_ctx_override = None


def _cmd_load(app, name: str, arg: str) -> None:
    if getattr(app.backend, "remote", False):
        app.system_message("Remote models need no loading. Select one with /model.")
        return
    if getattr(app.backend, "name", "") == "ninfer":
        # ⬜ A REFUSAL, NOT A RE-WORDING (T860). `/load` asks a server to bring a
        # model into memory; NInfer serves ONE artifact per process, fixed
        # before it starts, and has no `/models/load` at all. Re-phrasing the
        # error would still imply the verb exists here.
        app.system_message(
            "NInfer serves one artifact per process — there is nothing to load. "
            "/model picks the artifact, /engine start serves it."
        )
        return

    typed = arg.strip()
    interactive = not getattr(app, "_rpc", False) and bool(app.available_models)

    if not typed:
        # 🔴 RYAN, 2026-09-10 19:4x: "/load should show the model selector so the
        # user can select the modal to load not rely on them to type it
        # perfectly." No-arg used to silently load whatever `/model` had
        # selected, which is the one outcome a user asking for a menu does not
        # want — it acts without showing them the choice they came for.
        if interactive:
            pick(
                app,
                "Load a model",
                _load_rows(app),
                partial(_on_load_picked, app),
                current=app.model_id,
                extra_factory=partial(_load_ctx_row, app),
                on_pick=partial(_read_load_ctx, app),
            )
            return
        target = app.model_id
        if not target:
            app.system_message(
                f"No model selected — {_empty_state_hint(app)}")
            return
        _start_load(app, target)
        return

    # A typed name that is not a known model is a TYPO far more often than it is
    # a model the discovery missed — which is the whole reason this card exists.
    # Opening the picker with the near matches on top answers the question the
    # error line only restated.
    #
    # ⚠️ GATED ON `app.available_models` BEING NON-EMPTY. With no discovered
    # models there is nothing to compare against and nothing to show, and
    # refusing the load would break `/load <name>` for anyone whose backend
    # lists nothing — a strictly worse failure than the typo it guards.
    if interactive and typed not in app.available_models:
        near = difflib.get_close_matches(typed, app.available_models, n=5, cutoff=0.4)
        app.system_message(f"No model named {typed!r} — pick one:")
        pick(
            app,
            "Load a model",
            _load_rows(app, first=near),
            partial(_on_load_picked, app),
            current=app.model_id,
            extra_factory=partial(_load_ctx_row, app),
            on_pick=partial(_read_load_ctx, app),
        )
        return

    _start_load(app, typed)


def _cmd_unload(app, name: str, arg: str) -> None:
    if getattr(app.backend, "remote", False):
        app.system_message("Remote models do not occupy local model memory.")
        return
    target = arg.strip() or app.model_id
    if not target:
        app.system_message(f"No model selected — {_empty_state_hint(app)}")
        return

    async def _go() -> None:
        try:
            await app.backend.unload(target)
        except llm_backend.BackendError as e:
            app.system_message(str(e))
            return
        app.system_message(f"Unloaded: {target} — VRAM freed")
        if target == app.model_id:
            app.fetch_context_window()

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
    ("mmproj", "Vision projector (mmproj path, or \"none\")", "text", "off"),
    ("chat_template_file", "Chat template file", "text", "model's own"),
]

#: Inference tab rows (override keys). Blank = inherit the global /settings
#: value; the placeholder SHOWS what is inherited so blank is never a mystery.
_INFER_FIELDS: list[tuple] = [
    ("temperature", "Temperature", "float", None),
    ("top_p", "Top P Sampling", "float", None),
    ("top_k", "Top K Sampling", "int", None),
    ("min_p", "Min P Sampling", "float", None),
    ("repeat_penalty", "Repeat Penalty", "float", None),
    ("presence_penalty", "Presence Penalty", "float", None),
    ("frequency_penalty", "Frequency Penalty", "float", None),
    ("max_tokens", "Limit Response Length (tokens)", "int", None),
    ("stop", "Stop Strings (comma-separated)", "text", None),
    # Per-model thinking level: wins over the global /think for this
    # model, blank = inherit it. A request-time knob that rides
    # extra_body — works on BOTH backends, unlike the Load tab which is
    # llama.cpp's flag world.
    ("reasoning_effort", "Thinking Level (per-model)", "select",
     ["off", "minimal", "low", "medium", "high", "xhigh"]),
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


#: Declared once and installed on both the body and its screen; see
#: `scheduler_ui`'s `_CAL_KEYS` for why. `escape` is on the screen only —
#: `SidePanel` already binds it to cancel the dialog.
_MC_KEYS = [Binding("ctrl+s", "apply", "Apply", show=False)]


#: Upper bound on a typed context length. A TYPO GUARD, NOT A VRAM MODEL — and
#: the distinction is the whole design, so it is stated rather than implied.
#:
#: 🔴 A PER-MODEL MAXIMUM IS NOT AVAILABLE HERE. `ModelRow` carries no such
#: field, and the only real ceiling in the codebase — LM Studio's
#: `max_context_length`, read in `_model_info_sync` — lives behind a backend
#: round-trip that has no business inside a keypress handler. So this rejects
#: values that are ORDERS OF MAGNITUDE wrong (the walk's 819,250,000 is out by a
#: factor of ~780) and does not pretend to know what this model can hold.
#: Claiming "the max is 1048576" would be a number the code cannot stand behind.
#: 2**20 is chosen because no GGUF trains anywhere near it, so the guard cannot
#: reject a real setting — the failure it prevents is a mistyped digit, which is
#: exactly how it happened.
CTX_TYPO_CEILING = 1 << 20
CTX_FLOOR = 512


def validate_load_cfg(cfg: dict) -> None:
    """Raise `ValueError` naming the field and the reason, or return.

    ⚠️ `_collect_group` WAS NEVER GOING TO CATCH THIS. Its only numeric check is
    `int(raw)`, and "819250000" IS a valid int; its message ("is not a valid
    number") is true of the cases it catches and silent about the case that
    matters — a number that parses and is absurd. This runs AFTER it, on the
    assembled cfg, because that is the last point before the value reaches
    `settings_mod.save` and a model reload.
    """
    ctx = cfg.get("ctx")
    if ctx is not None:
        if not isinstance(ctx, int) or isinstance(ctx, bool):
            raise ValueError(f"ctx: {ctx!r} is not a whole number")
        if not (CTX_FLOOR <= ctx <= CTX_TYPO_CEILING):
            raise ValueError(
                f"ctx: {ctx:,} is not a plausible context length "
                f"(expected {CTX_FLOOR:,}–{CTX_TYPO_CEILING:,}). "
                "This is a typo guard, not your model's real maximum — "
                "a value inside the range can still be more than the GPU holds."
            )

    mmproj = cfg.get("mmproj")
    if mmproj is not None and not llm_backend.is_no_projector(mmproj):
        # 🔴 SAY WHICH CHECK FAILED, BOTH OF THEM. The walk's value was
        # "none" typed in FRONT of a path: it is neither the T245 sentinel
        # (an exact, stripped, case-insensitive match) NOR a file that exists.
        # Naming only one sends the reader to check the wrong thing.
        try:
            exists = Path(str(mmproj)).is_file()
        except OSError:
            exists = False
        if not exists:
            raise ValueError(
                f"mmproj: {mmproj!r} is neither \"none\" nor a file that exists"
            )


class ModelConfigBody(Widget):
    """Info / Load / Inference for ONE model — LM Studio's panel, in the TUI.

    Unknown keys already present in the stored dicts are PRESERVED verbatim
    on save (forward compatibility: a newer LiteTUI's setting must survive a
    round-trip through an older screen, silent data loss being the one
    forbidden outcome).

    🔴 `height: 100%` AND `#set-box` KEEPS ITS OWN `height: 88%`. That box is
    SHARED with `SettingsScreen`, which is not converted yet, so moving the
    percentage up here — the shape /help used — would have silently shortened
    the settings panel to 88% of 88%. A body that is exactly the screen is
    TRANSPARENT to every percentage inside it, which is the safer half of the
    rule and the one to reach for when a box has more than one owner.
    """

    DEFAULT_CSS = """
    ModelConfigBody { width: 100%; height: 100%; align: center middle; layout: vertical; }
    """

    BINDINGS = list(_MC_KEYS)

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    # -- compose -----------------------------------------------------------

    def _load_cfg(self) -> dict:
        return dict(self.app.settings.llama_load_settings.get(self._key, {}))

    def _infer_cfg(self) -> dict:
        return dict(self.app.settings.model_infer_overrides.get(self._key, {}))

    def _sibling_projector(self) -> tuple[str | None, list[str]]:
        """The projector auto-pairing would use for THIS model, and all found.

        Reads `app.model_rows`, which the app already holds — not a fresh scan.
        A directory walk inside `compose()` would put a filesystem crawl on the
        path that draws the panel, and this screen opens from a keypress.
        """
        row = self.app.model_rows.get(self._key)
        return llm_backend.sibling_mmproj(getattr(row, "path", None))

    def compose(self) -> ComposeResult:
        app = self.app
        on_llama = app.backend.name == "llamacpp"
        # 🔴 T806 — THE LABEL WAS A TWO-WAY CHOICE IN A FOUR-BACKEND APP, so
        # every backend that was not llama.cpp was captioned "LM Studio". On
        # NInfer the Model screen said LM Studio at the top while refusing every
        # LM Studio verb underneath. Read the backend's own name instead of
        # inferring it from one comparison.
        engine_label = llm_backend.backend_label(app.backend.name)
        flags = llm_backend.configured_flags(app.settings)
        load_cfg = self._load_cfg()
        infer_cfg = self._infer_cfg()
        row = app.model_rows.get(self._key)

        with Vertical(id="set-box"):
            engine = engine_label
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
                                mods = ", ".join(row.modalities)
                                # 🔴 DECLARED vs EFFECTIVE. `row.modalities` is
                                # what the model ARCHITECTURE reports; with the
                                # projector explicitly off it cannot act on an
                                # image whatever it declares, and a panel that
                                # says "text, image" beside a setting that
                                # forbids images is a panel that lies.
                                if llm_backend.is_no_projector(
                                        load_cfg.get("mmproj")):
                                    mods += "  ->  text only (projector: none)"
                                yield Static(f"Modalities: {mods}",
                                             classes="set-help")
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
                        auto_mmproj, mmproj_found = self._sibling_projector()
                        for key, label, kind, extra in _LOAD_FIELDS:
                            editable = on_llama or key == "ctx"
                            flag = llm_backend.FLAG_FOR.get(key, "")
                            missing = bool(flags) and flag not in flags
                            note = (
                                f"n/a in installed build ({llm_backend.configured_build(app.settings)})"
                                if missing else
                                ("" if editable else "LM Studio manages this")
                            )
                            if (key == "mmproj" and not missing and editable
                                    and llm_backend.is_no_projector(
                                        load_cfg.get("mmproj"))):
                                # The field already SHOWS "none" (it renders the
                                # stored value), so the placeholder never
                                # appears — but the help line must not go on
                                # describing an auto-pair that this model has
                                # been told not to do.
                                note = note or (
                                    "text only by your choice; clear the field "
                                    "to go back to pairing the sibling"
                                )
                            elif key == "mmproj" and not missing and editable:
                                # 🔴 THE PLACEHOLDER IS THE RESOLVED DEFAULT, not
                                # the word "off". "off" was a lie the moment
                                # auto-pairing existed: blank now MEANS the
                                # sibling below, and a field whose empty state
                                # does something has to say what.
                                if auto_mmproj:
                                    extra = f"auto: {Path(auto_mmproj).name}"
                                    note = note or (
                                        "blank uses the projector found beside the "
                                        "model; type a path to override"
                                    )
                                elif len(mmproj_found) > 1:
                                    extra = f"{len(mmproj_found)} beside the model — none auto"
                                    note = note or (
                                        "two or more projectors share this directory, "
                                        "so none is guessed: "
                                        + ", ".join(Path(f).name for f in mmproj_found)
                                    )
                            yield from self._field_rows(
                                "ld", key, label, kind, extra,
                                load_cfg.get(key),
                                disabled=(not editable) or missing,
                                note=note,
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
                        for row in _INFER_FIELDS:
                            key, label, kind = row[0], row[1], row[2]
                            choices = row[3] if len(row) > 3 else None
                            inherited = getattr(app.settings, key, None)
                            blank_label = "server default"
                            note = ""
                            if key == "stop":
                                inherited = ",".join(app.settings.stop) or None
                            if key == "max_tokens":
                                inherited = (
                                    app.settings.max_tokens_tools
                                    if app.tools_enabled else app.settings.max_tokens_chat
                                )
                            if key == "reasoning_effort":
                                # Inherits the GLOBAL thinking level and applies to
                                # chat AND compaction — not a server default.
                                inherited = app.thinking_level or None
                                blank_label = (
                                    f"inherit global ({app.thinking_level})"
                                    if app.thinking_level else "inherit global (unset)"
                                )
                                note = ("applies to chat + compaction; off sends none "
                                       "(no reasoning trace)")
                            yield from self._field_rows(
                                "inf", key, label, kind,
                                choices if choices is not None else
                                    (f"inherited: {inherited}" if inherited is not None else "server default"),
                                infer_cfg.get(key),
                                note=note, blank_label=blank_label,
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
            # 🔴 OUTSIDE THE TABS, LIKE THE SWAP CONTROL AND FOR A SHARPER
            # REASON: a refusal about a field on the Load tab has to be readable
            # while you are LOOKING at that tab, and a Static inside one TabPane
            # is gone the moment you change tab. Empty until something fails.
            # (`#set-error` in settings_screen.py:605 carries a comment about
            # being yielded unconditionally so `query_one` always finds it —
            # same reason here.)
            yield Static("", id="mc-error")
            # Outside the tabs, on its own line: this dialog has no button row
            # to sit in, and a control inside one TabPane would vanish when the
            # user changed tab — visible on Info, gone on Load.
            yield SwapButton()

    def _field_rows(self, prefix: str, key: str, label: str, kind: str, extra,
                    current, disabled: bool = False, note: str = "",
                    blank_label: str = "server default"):
        wid = f"{prefix}-{key}"
        with Vertical(classes="set-row"):
            yield Label(label, classes="set-label")
            if kind == "tri":
                choices = [(blank_label, ""), ("on", "true"), ("off", "false")]
                value = "" if current is None else ("true" if current else "false")
                yield Select(choices, value=value, id=wid,
                             allow_blank=False, disabled=disabled)
            elif kind == "select":
                choices = [(blank_label, "")] + [(c, c) for c in extra]
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

    # -- state carry across a live host swap --------------------------------

    def get_state(self) -> dict:
        """Every editable control, by id.

        🔴 DERIVED FROM THE DOM, NOT FROM A LIST OF FIELDS. The ids come from
        `_field_rows` (`ld-*`, `inf-*`) and three fixed ones, and `_LOAD_FIELDS`
        / `_INFER_FIELDS` are ALREADY the source for those — but which of them
        actually render depends on the backend, the installed build's flag
        census, and the model row. A hand-kept second list would silently drop
        whatever the census hid, and "the field you could not see is the field
        that emptied" is exactly the failure a swap must not produce.
        """
        out: dict = {}
        for w in self.query(Input):
            if w.id:
                out[w.id] = w.value
        for w in self.query(Select):
            if w.id:
                out[w.id] = w.value
        return out

    def set_state(self, state: dict) -> None:
        for wid, value in (state or {}).items():
            found = self.query(f"#{wid}")
            if not found:
                continue          # a control this host does not render
            try:
                found.first().value = value
            except Exception:
                continue          # a Select whose options no longer hold it

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
            validate_load_cfg(load_cfg)
        except ValueError as e:
            # 🔴 THE PANEL STAYS OPEN AND SAYS WHY, RATHER THAN CLOSING AND
            # WHISPERING INTO THE CHAT. A refused save that dismissed the dialog
            # would cost the user every other field they had edited, and the
            # reason would be one line up in a transcript they are not looking
            # at. `system_message` is kept as well: the chat is where a user
            # scrolls back to ask "what did I do".
            self._say_error(f"Cannot apply — {e}")
            app.system_message(f"Not applied — {e}")
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
        settings_runtime.persist_or_raise(app, s)

        key = self._key
        if app.backend.name == "llamacpp" and load_cfg != prior_load:
            async def _apply() -> None:
                # T873, same shape: `_apply_sync` opens with
                # `_refuse_if_attached` and `_regen_ini` refuses an adopted
                # router after it, so the line fires from the backend once both
                # have passed.
                try:
                    await app.backend.apply_load_settings(
                        key, load_cfg,
                        notice=_notice(app, f"Applying load settings to {key} (reload)…"))
                except llm_backend.BackendError as e:
                    app.system_message(str(e))
                    return
                app.system_message(f"{key}: load settings applied")
                # ⬜ REFRESH THE ROWS, OR THE INFO TAB LIES ABOUT WHAT JUST
                # HAPPENED. Seen on the walk: Info read "Not loaded" while the
                # router reported the model loaded, because `app.model_rows` is
                # only rebuilt by `connect()` and nothing here called it —
                # `fetch_context_window()` updates the WINDOW, not the `loaded`
                # marker the Info tab reads. `_cmd_load`'s worker already does
                # this for the same reason (see `_go`).
                app.connect()
                if key == app.model_id:
                    app.fetch_context_window()
            app.run_worker(_apply(), group="modelctl", exclusive=True)
        elif app.backend.name == "lmstudio" and load_cfg.get("ctx") != prior_load.get("ctx"):
            async def _apply_lms() -> None:
                try:
                    await app.backend.apply_load_settings(key, {"ctx": load_cfg.get("ctx")})
                except llm_backend.BackendError as e:
                    app.system_message(str(e))
                    return
                app.system_message(f"{key}: context length applied")
                if key == app.model_id:
                    app.fetch_context_window()
            app.run_worker(_apply_lms(), group="modelctl", exclusive=True)
        elif load_cfg != prior_load:
            # 🔴 T806 — WITHOUT THIS ARM THE SCREEN ACCEPTED THE CHANGE AND DID
            # NOTHING. Two name branches in a four-backend app means every other
            # backend falls off the end: the settings were SAVED to disk above
            # and never sent anywhere, with no message either way.
            #
            #     RYAN, on the Model Hub: *"its saying install ninfer and
            #     download the model still ... but all 3 buttons are unclickable
            #     showing a general prohibition sign"*. This is the same defect
            #     one app along — a control that looks like it worked.
            #
            # ⬜ ASK THE BACKEND AND SHOW WHAT IT SAYS. NInfer refuses by name
            # ("every setting is fixed at startup ... change it in LiteSuite's
            # Model Hub and restart the engine there"), so routing through the
            # callee turns a silent no-op into the reason. A backend that CAN
            # apply them applies them, with no new branch here.
            async def _apply_backend() -> None:
                try:
                    await app.backend.apply_load_settings(key, load_cfg)
                except llm_backend.BackendError as e:
                    app.system_message(str(e))
                    return
                app.system_message(f"{key}: load settings applied")
                if key == app.model_id:
                    app.fetch_context_window()
            app.run_worker(_apply_backend(), group="modelctl", exclusive=True)

        app.system_message(f"Saved model config for {key}")
        close_dialog(self, None)

    def _say_error(self, text: str) -> None:
        """Put a refusal where the person can see it. Never raises: a panel that
        crashed while reporting a validation error would be worse than the
        unvalidated save this exists to prevent."""
        found = self.query("#mc-error")
        if found:
            found.first().update(text)

    def action_cancel(self) -> None:
        close_dialog(self, None)


class ModelConfigScreen(ModalScreen[None]):
    """The model panel, as a modal. The content lives in `ModelConfigBody`."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False), *_MC_KEYS]

    def __init__(self, key: str) -> None:
        super().__init__()
        self._key = key

    def compose(self) -> ComposeResult:
        yield ModelConfigBody(self._key)

    def action_apply(self) -> None:
        self.query_one(ModelConfigBody).action_apply()

    def action_cancel(self) -> None:
        self.dismiss(None)


def _cmd_modelcfg(app, name: str, arg: str) -> None:
    if getattr(app.backend, "remote", False):
        target = arg.strip() or app.model_id
        levels = app.backend.reasoning_levels(target)
        if not levels:
            app.system_message("Select an available Codex model with /model first.")
            return
        def selected(level):
            if level:
                app.settings.model_infer_overrides.setdefault(target, {})["reasoning_effort"] = level
                settings_runtime.persist_or_raise(app, app.settings)
                if target == app.model_id:
                    app.thinking_level = level
                    app.update_header()
        pick(app, "Codex reasoning effort (response limits are provider-managed)",
             [(level, level) for level in levels], selected, current=app.thinking_level)
        return
    target = arg.strip() or app.model_id
    if not target:
        app.system_message(f"No model selected — {_empty_state_hint(app)}")
        return
    present_dialog(app, partial(ModelConfigBody, target),
                   partial(ModelConfigScreen, target))


# ── First-boot engine picker ─────────────────────────────────────────────────

def _activate(app) -> None:
    """Exactly once, and only when BOTH engines are detected, ask which one
    this seat should use. One engine → chosen silently. Esc → not marked
    chosen, so the question returns next boot (a dismissed question is not an
    answer). Detection is deliberately offline-cheap: an exe on disk and a
    CLI on PATH — no network probes on the boot path."""
    s = app.settings
    if s.backend in model_transport.OAUTH_PROVIDERS:
        return
    if s.backend_chosen:
        return
    if settings_mod.source_of("backend"):
        return   # the environment already chose; a question would be a lie
    lms_present = bool(shutil.which("lms"))
    llama_present = llm_backend.llama_available(s) if getattr(s, "llama_executable", "") else llm_backend.llama_available()
    if not lms_present and not llama_present:
        return   # nothing to choose between; ask when one appears
    if lms_present != llama_present:
        s.backend = "llamacpp" if llama_present else "lmstudio"
        s.backend_chosen = True
        settings_runtime.persist_or_raise(app, s)
        if s.backend != app.backend.name:
            app.backend = llm_backend.make_backend(s)
            app.connect()
        return

    rows = [
        ("lmstudio", "LM Studio desktop — the lms.exe you already run"),
        ("llamacpp", f"llama.cpp — our own engine ({llm_backend.configured_build(app.settings)})"),
    ]

    def _picked(choice: str | None) -> None:
        if choice is None:
            return   # ask again next boot
        s.backend = choice
        s.backend_chosen = True
        settings_runtime.persist_or_raise(app, s)
        if choice != app.backend.name:
            app.backend = llm_backend.make_backend(s)
            app.model_id = ""
        app.update_header()
        app.connect()

    pick(app, "Which engine should serve this seat?", rows, _picked, current=s.backend)


# ── Registration ─────────────────────────────────────────────────────────────

def _register(ctx) -> None:
    ctx.command(
        ("/model", "/models"), _cmd_model,
        palette="Switch model",
        help="Pick a different model to answer with.",
        group="backend",
        order=10,
    )
    ctx.command(
        ("/reconnect",), _cmd_reconnect,
        palette="Reconnect",
        help="Lost the model server? Try again.",
        group="backend",
        order=60,
    )
    from litetui import gpu_gate

    if gpu_gate.is_rtx_5090():   # T893: the command is not shown, not even as a refusal
        ctx.command(
            ("/engine",), _cmd_engine,
            palette="NInfer engine",
            help="Start (optionally with N lanes), stop or check the NInfer engine LiteTUI may own; lanes N sets its --max-concurrency.",
            group="backend",
            order=55,
        )
    ctx.command(
        ("/backend",), _cmd_backend,
        palette="Switch backend",
        help="Choose a local engine, custom endpoint, Codex, or Claude Agent.",
        group="backend",
        order=50,
    )
    ctx.command(
        ("/load",), _cmd_load,
        palette="Load model",
        help="Put a model into memory so it is ready to answer.",
        group="backend",
        order=30,
    )
    ctx.command(
        ("/unload",), _cmd_unload,
        palette="Unload model",
        help="Free the graphics memory a model is holding.",
        group="backend",
        order=40,
    )
    ctx.command(
        ("/modelcfg",), _cmd_modelcfg,
        palette="Model config",
        help="Tune how a model loads and how it answers.",
        group="backend",
        order=20,
    )


PLUGIN = PluginManifest(id="model-switch", register=_register, activate=_activate)
