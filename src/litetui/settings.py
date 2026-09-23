"""Settings — every knob in one typed, persisted place.

WHY THIS EXISTS
Before this file the knobs were scattered across three incompatible kinds of
place, and two of them could not be changed without editing source:

  1. `os.environ` at import time  — LM_TOOL_ITERS, LITETUI_LM_HOST
  2. module constants             — COMPACT_MAX_TOOL_ITERS, COMPACT_KEEP_RECENT
  3. literals inside the request  — max_tokens 16384/4096, compact's 2048

Class 3 is the dangerous one. A hardcoded number is not a default, it is a
WALL, and it announces itself only by the run it kills:

    [stopped — reached 48 tool iterations in one turn]
    Compact failed — no summary produced (gave up after 8 tool rounds).

Neither number was reachable from the UI. The first is at least env-overridable;
the second and the compact `max_tokens: 2048` behind it were not overridable at
all.

PRECEDENCE: env > settings file > default.
Env stays on top so the existing LM_TOOL_ITERS / LITETUI_LM_HOST knobs keep
working exactly as they did, and so a one-off override does not silently
rewrite a saved preference. A value that came from the environment is reported
as such by `source_of()` — the settings screen shows it as locked rather than
letting you "change" a value that the env will immediately win back. A control
that appears to work and does not is worse than one that is visibly disabled.

UNSET vs ZERO: `None` means "not configured, use the default". Several of these
(seed, top_k, stop) have meaningful zero/empty values, so absence is stored as
absence rather than as 0 or "".
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from litetui import skills as skills_mod  # for DEFAULT_EXTRA_ROOTS only
from litetui.tool_policy import AUTONOMOUS

SETTINGS_FILENAME = "settings.json"

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]

# One vocabulary for the footer renderer, keyboard navigation, and settings
# editor. Authority and plan are intentionally included: they remain always
# visible, but their position is still part of the user's left-to-right layout.
FOOTER_ORDER_DEFAULT: tuple[str, ...] = (
    "authority",
    "plan",
    "seat",
    "think",
    "bg",
    "agents",
    "convo",
    "ctx",
    "pct",
    "tps",
)


def normalize_footer_order(value: Any) -> list[str]:
    """Return a safe, complete footer order from user or disk input.

    Older settings files and hand-edited files may omit the new field, contain
    duplicate ids, or have an unknown id. Unknown/duplicate entries are
    ignored and omitted known entries are appended in the historical order, so
    a bad preference can never hide a footer field or break the app.
    """
    if not isinstance(value, (list, tuple)):
        value = ()
    known = set(FOOTER_ORDER_DEFAULT)
    seen: set[str] = set()
    result: list[str] = []
    for raw in value:
        item = str(raw).strip()
        if item in known and item not in seen:
            result.append(item)
            seen.add(item)
    result.extend(item for item in FOOTER_ORDER_DEFAULT if item not in seen)
    return result


@dataclass
class Settings:
    """Every user-facing knob. Field names are the JSON keys."""

    # ── Connection ───────────────────────────────────────────────────────────
    lm_host: str = "http://localhost:1234"
    #: T806 delta — an EXPLICIT NInfer address ("http://127.0.0.1:49260"). Empty
    #: means discover it from LiteSuite's config (extraEndpoints). Set this when
    #: ninfer-serve was started by hand and no LiteSuite is running: LiteTUI
    #: still only ATTACHES — this names an engine, it never starts one.
    ninfer_host: str = ""
    #: /engine start (Ryan a-35456da0 "LiteTUI may start it"): blank = LiteSuite's
    #: install (~/.litesuite/llm/ninfer/ninfer-serve.exe) and the ONE .ninfer it
    #: pulled. Set these for a standalone install. Context is the engine's
    #: --max-context (LiteSuite's ruling: 32k, fp8 KV).
    ninfer_executable: str = ""
    ninfer_artifact: str = ""
    ninfer_max_context: int = 32768
    #: The engine's --max-concurrency (1..8, serving.md:760): requests decoded in
    #: ONE batch — LM Studio's "Parallel" (Ryan 2026-09-18: "does are ninfer backend
    #: support parallel calls yet ? like lmstudio"). Lanes SHARE the --max-context KV
    #: pool and a request is admitted only when its whole reservation fits
    #: (serving.md:929-933, :949-955): no extra VRAM, less context each under load.
    #: A startup flag — applies on the next /engine start.
    ninfer_max_concurrency: int = 1

    # ── Model ────────────────────────────────────────────────────────────────
    #: Selected automatically on connect when present in the served list.
    #: None = use whatever LM Studio reports as loaded.
    default_model: str | None = None
    #: Ask LM Studio to load the model with this context length (tokens).
    #: None = leave the server's own configured length alone.
    default_context_length: int | None = None
    #: Re-apply default_model on every connect, not just the first.
    pin_default_model: bool = False

    # ── Backend (which engine serves the chat) ───────────────────────────────
    #: "lmstudio" (LM Studio desktop at lm_host), "llamacpp" (our own
    #: llama-server in router mode — the engine LiteSuite's Model Hub installs),
    #: "ninfer" (T806: the NVFP4 5090 engine, ATTACHED — LiteSuite starts it and
    #: LiteTUI never does) or "codex". The default preserves existing behavior
    #: exactly.
    backend: str = "lmstudio"
    #: The first-boot picker ran (it shows once, and only when BOTH engines
    #: are detected). Esc leaves this False so the question returns.
    backend_chosen: bool = False
    #: Codex only. False (default) = LiteTUI's own agent loop drives Codex over
    #: the Responses API — our tools, our compaction, our conversations, with
    #: prompt_cache_key sent on every request. True = the official app-server
    #: owns the loop, tools, history and compaction (0.23.0 behaviour).
    #: Takes effect on /reconnect or the next launch.
    codex_native_engine: bool = False
    #: OUR router instance. 7470 sits in the ecosystem's 74xx block — the
    #: 8xxx range is crowded on dev machines (Ryan, 2026-08-21).
    custom_base_url: str = ""
    custom_api_key_env: str = ""
    custom_context_length: int = 0
    llama_host: str = "http://localhost:7470"
    #: Explicit installed executable. Empty selects the ecosystem default.
    llama_executable: str = ""
    #: Probed IN ORDER before spawning; a healthy answer means attach, never
    #: spawn. Default is LiteSuite's own single-model server. An attached
    #: server belongs to whoever started it: we chat through it and refuse to
    #: manage its models.
    #:
    #: 8088 STAYS, and 7470 is deliberately NOT here. 8088 is where older
    #: LiteSuite installs still serve, so removing it would strand them. 7470
    #: is our own port, reached earlier in the attach order — a LiteSuite
    #: router there is recognised by `router.json`, not by this list, and
    #: adding it would shadow that check with a blind attach that could never
    #: tell a live owner from our own crashed orphan.
    llama_attach_hosts: list[str] = field(
        default_factory=lambda: ["http://localhost:8088"]
    )
    #: Discovery scan roots. Every GGUF on the box is one picker, not four.
    llama_scan_litesuite: bool = True   # ~/.litesuite/llm/models
    llama_scan_lmstudio: bool = True    # ~/.lmstudio/models (+ legacy cache dir)
    llama_scan_hf_cache: bool = True    # $HF_HOME or ~/.cache/huggingface/hub
    llama_models_dirs: list[str] = field(default_factory=list)
    #: --models-max for OUR router. 2, not upstream's 4: two large models
    #: already fill this class of GPU, and the OOM of 2026-08-21 is why
    #: memory ceilings are set here rather than discovered by crashing.
    llama_models_max: int = 2
    #: Per-model Load-tab overrides {model_key: {"ctx": …, "ngl": …, ...}} —
    #: key vocabulary is llm_backend.FLAG_FOR, edited via /modelcfg.
    llama_load_settings: dict = field(default_factory=dict)
    #: Per-model Inference-tab overrides, BOTH backends {model_key: {...}}.
    #: Unset field = inherit the global sampling settings below.
    model_infer_overrides: dict = field(default_factory=dict)
    #: Named presets {name: {"load": {...}, "inference": {...}}} — saved and
    #: applied from /modelcfg.
    llama_presets: dict = field(default_factory=dict)
    #: lmstudio SDK sync-API timeout. Its default (60s) is shorter than a
    #: large model's load; a timeout mid-load reads as a failure that isn't.
    lms_load_timeout_s: int = 600

    # ── Generation ───────────────────────────────────────────────────────────
    #: Response budget WITH tools enabled. The agent loop needs headroom for
    #: tool calls plus prose; 16384 was the hardcoded value.
    max_tokens_tools: int = 16384
    #: Response budget with tools off.
    max_tokens_chat: int = 4096
    thinking_level: ThinkingLevel = "medium"
    #: LM Studio model ids whose GRADED thinking levels are real (T539-A).
    #:
    #: On the LM Studio backend a graded reasoning_effort is silently dropped
    #: for a model that carries no reasoning-level mapping -- the request
    #: returns 200 and the model reasons at the server default -- so the app
    #: collapses graded levels to on/off there (turn_engine._resolve_reasoning_
    #: effort). Measured 2026-09-08: on qwen3.8-27b-nvfp4-mtp all five graded
    #: values were BYTE-IDENTICAL to sending no field; on qwen/qwen3.8-27b they
    #: produced four distinct behaviours (none/low/medium/xhigh, with minimal
    #: snapped to low and high to xhigh by LM Studio itself).
    #:
    #: This list is the exception, and it is a SETTING because which build is
    #: official is the human's knowledge: /api/v0/models exposes no reasoning
    #: capability on ANY of the 16 local models, so it cannot be derived.
    #: Ids are matched EXACTLY (case-insensitive) -- never as substrings, since
    #: "qwen3.8-27b-nvfp4-mtp" contains "qwen3.8-27b" and would be allowlisted
    #: by a substring rule. That means an id here must be the id LM Studio
    #: serves the model under: `lms load <key>` with no --identifier uses the
    #: key itself, a custom --identifier replaces it.
    #: Irrelevant on the llamacpp backend, where every level is sent verbatim.
    lmstudio_graded_thinking_models: list[str] = field(
        default_factory=lambda: ["qwen/qwen3.8-27b"]
    )

    # LM Studio sampling flags. None = omit the field entirely and let the
    # server use its own default — NOT the same as sending a zero.
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    seed: int | None = None
    #: Stop strings. Empty list = omit.
    stop: list[str] = field(default_factory=list)

    # ── Agent loop ───────────────────────────────────────────────────────────
    #: Safety cap on tool round-trips in one turn. This is the number behind
    #: "[stopped — reached N tool iterations in one turn]".
    tool_iterations: int = 48
    #: T517 — a FOREGROUND tool call still running after this many seconds is moved
    #: to a background task on its own (the result arrives later as a message). 0 = never.
    tool_auto_background_s: int = 30
    #: T538 — the model the subagent tool sends its child to when the call names
    #: none. None = the parent's own model. Ryan 2026-09-08 21:4x: "run it with
    #: the 27b using the 2B Q4 as its subagents" — a small model loaded beside
    #: the big one has its own LM Studio slots, so summaries and extractions run
    #: there without touching the parent's pool.
    subagent_model: str | None = None
    #: T640 — the model the `llm-tool-summ` fold's throwaway side call goes to.
    #: None = the model you are talking to, which is what it always did.
    #:
    #: 🔴 IT HAD NO SETTING AND THE DEFAULT WAS INVISIBLE. The fold sent
    #: `model=self.model_id or "local-model"` — the MAIN model — so on Codex
    #: every folded tool result was a Codex call nobody chose, and on a local
    #: backend it competed for the big model's own slots. Ryan 2026-09-11 15:1x:
    #: MiniCPM5-2B is resident and already the subagent default; the fold is the
    #: other side call and should be pointable at the same small model.
    #:
    #: ⚠️ A COLD PICK IS NEVER LOADED TO SATISFY THIS. It falls back to the main
    #: model with a note — see `model_residency.resolve_side_call_model`.
    tool_summary_model: str | None = None
    tools_enabled: bool = True
    #: Authority profile for EVERY turn -- typed, inbox-woken, cron and loop
    #: alike (Ryan: "cron and loops run at same set profile level my ruling").
    #: The host, never the model, applies this at each tool call. Cycled live
    #: with shift+tab; the footer always names the level in force.
    #:
    #: 🔴 THE DEFAULT IS `autonomous`, AND THAT IS A RULING, NOT AN OVERSIGHT.
    #: Ryan was asked as an explicit either/or -- degrade unattended turns to
    #: read-only, or default to auto -- and answered "b default to auto",
    #: modelled on Claude Code, whose own default mode is called `auto`. The
    #: permissiveness was stated in the option he chose from: a user who never
    #: opens Settings gets workspace_write on an unattended turn.
    #:
    #: ⚠️ `autonomous` IS SAFE TO RUN UNATTENDED FOR A REASON THAT IS EASY TO
    #: BREAK: its `confirm` set is EMPTY, so `evaluate` never constructs a
    #: modal for it. That is what keeps an unattended turn from blocking on a
    #: human who is not there -- not the profile's name. A profile that both
    #: ran unattended and could confirm would hang the turn forever.
    #:
    #: 📌 An explicitly chosen `interactive` still meets unattended turns, and
    #: those still go through `tool_policy.unattended()`, which degrades a
    #: confirm-based level to the read-only floor. The default moved; that
    #: guard did not become unnecessary.
    tool_policy_profile: str = AUTONOMOUS
    #: The human's standing answers to the approval modal, keyed by
    #: `tool_policy.rule_key` -- "<tool>:<sorted,capabilities>", NOT the tool
    #: name alone. Written by the modal's "Always allow" button.
    #: `list[str]` and not `frozenset` because `_coerce` handles list[str] and
    #: would silently drop a set; converted at the call site in app.py.
    tool_always_allow: list[str] = field(default_factory=list)
    #: Standing refusals, same key shape. Consulted BEFORE the profile and
    #: before any allow rule: a refusal the human wrote down wins over
    #: everything. No UI writes this yet -- settings.json only, by hand.
    tool_deny: list[str] = field(default_factory=list)
    #: Per-tool denylist by tool NAME — the boxes unticked in `/tools`.
    #: A DENYLIST and not an allowlist, mirroring `mcp_disabled_servers`: with
    #: an allowlist a newly registered tool is INVISIBLE until someone notices
    #: and adds it, and a capability that silently fails to appear is far worse
    #: than one that appears and gets unticked.
    #: Two mechanisms honour it — the schema is withheld from `tool_specs()`,
    #: AND the call is refused at the authorization door, because a name
    #: remembered from earlier in the same conversation still reaches the host.
    tools_disabled: list[str] = field(default_factory=list)

    # ── Tool output context (tool_context.py) ────────────────────────────────
    #: What a tool's output contributes to the conversation: "off" puts the raw
    #: in verbatim (the baseline), "llm-tool-mask" replaces it with a pointer
    #: placeholder, "llm-tool-summ" replaces it with a side-call summary. Both
    #: processing modes park the raw in a sidecar file under the conversation's
    #: own directory — nothing is destroyed, the model can `read` it back. The
    #: literature disagrees about mask vs summarise (see tool_context.py), so
    #: this is a switch to be measured, not a decision.
    tool_context_mode: str = "off"
    #: Results under this size enter verbatim whatever the mode: the raw is
    #: smaller than the machinery around it, and a summary can be LONGER than
    #: what it replaces.
    tool_context_threshold_chars: int = 2000

    # ── Mid-turn input (message queue) ───────────────────────────────────────
    #: The Enter <-> ctrl+shift+enter mapping for a message sent WHILE the
    #: agent is mid-turn. The two BEHAVIOURS trade places (Ryan, 2026-08-21 —
    #: "swapping the default behavior between those two in the settings page"):
    #:   False (default): Enter QUEUES the message · ctrl+shift+enter INTERRUPTS
    #:   True  (swapped): Enter INTERRUPTS          · ctrl+shift+enter QUEUES
    #: Queue = hold until the turn ends, then send as a normal turn.
    #: Interrupt = stop the turn (partial kept), then send yours.
    #: A message sent while idle just sends, whatever this says.
    enter_interrupts: bool = False

    # ── Compaction ───────────────────────────────────────────────────────────
    compact_max_tool_iters: int = 8
    #: 🔴 WAS HARDCODED AT 2048 AND THAT IS WHY /compact PRODUCED NO SUMMARY.
    #: Compact asks for reasoning_effort="none", but a virtual model whose own
    #: level set lacks "none" has the field DROPPED with a 200 (see app.py's
    #: _warn_reasoning_ignored) and reasons at the server default instead. The
    #: reasoning then eats the entire budget and the summary is never emitted —
    #: reported as "no summary produced", which reads like a retry problem and
    #: is not one. qwen3.8-27b was measured converging at ~7,212 tokens.
    compact_max_tokens: int = 12288
    compact_keep_recent: int = 4
    #: Effort used for the compaction call. "off" is the cheap ideal but is the
    #: value that gets silently dropped; see compact_max_tokens.
    compact_thinking_level: ThinkingLevel = "low"

    # ── Auto-compaction ──────────────────────────────────────────────────────
    autocompact_enabled: bool = True
    #: Percent of the context window at which compaction fires by itself.
    #: Needs headroom: compaction is itself a request, and one that runs at 99%
    #: has no room to produce the summary that would have saved the session.
    autocompact_at_percent: int = 80


    #: After every SUCCESSFUL compaction, ping the model like a user message
    #: so an in-flight task resumes instead of the model sitting on the
    #: summary. This is loop mode: an auto-compact at 80% used to park a long
    #: agent run silently, because nothing asks for anything once the context
    #: has been replaced. The ping buys one cheap round-trip - resume the
    #: task, or say standing by and stop.
    #:
    #: Ships OFF: it changes what /compact does for every user and costs a
    #: model round-trip when there is nothing to resume. Turn it ON in
    #: settings.json (or /settings) for long agentic sessions.
    wake_after_compact: bool = False

    #: Clear the RENDERED transcript after a compaction.
    #:
    #: Not cosmetic. After compacting, the chat log still shows every message
    #: that was just REPLACED by the summary — so the screen displays history
    #: the model can no longer see, and the two silently disagree. Anyone
    #: scrolling up is reading context that is gone. Clearing makes the visible
    #: transcript mean what it appears to mean.
    clear_screen_after_compact: bool = True

    # ── Fleet ────────────────────────────────────────────────────────────────
    #: The name this seat asks the harness registry for.
    #:
    #: Was hardcoded "LiteTUI" in app.py, so every instance asked for the same
    #: literal and the roster could not tell two of them apart. It is a NAME,
    #: not an id: mail is addressed by agent_id, so a refused or taken name
    #: misleads humans reading `discover` without misdelivering anything.
    #: Blank falls back to "LiteTUI" rather than to a generated name — a seat
    #: whose name changes every launch is what this replaced.
    seat_name: str = "LiteTUI"

    # ── Capabilities ─────────────────────────────────────────────────────────
    skills_enabled: bool = True
    #: Extra skill libraries, scanned after the repo's own `skills/`.
    #:
    #: Directories of skills (each holding `<name>/SKILL.md`). `~` expands and
    #: `*` picks the NEWEST match, so a versioned plugin path follows the
    #: plugin instead of pinning one release — two literal paths in this repo
    #: rotted exactly that way on 2026-08-20.
    #:
    #: Only the INDEX LINE of each skill reaches the model (name + one-line
    #: description, ~4k tokens for 77 skills); bodies load on demand through
    #: the `skill` tool. Emptying this leaves only the repo's own skills.
    skill_roots: list[str] = field(
        default_factory=lambda: list(skills_mod.DEFAULT_EXTRA_ROOTS)
    )
    mcp_enabled: bool = True
    #: Server names from mcp.json to NOT start. Absent = start everything.
    mcp_disabled_servers: list[str] = field(default_factory=list)
    #: Plugin ids to NOT load at boot (applies at next start; the critical
    #: core-tools plugin ignores this — a harness that cannot read a file
    #: is a lying boot). Mirrors mcp_disabled_servers.
    plugins_disabled: list[str] = field(default_factory=list)

    # ── Interface ────────────────────────────────────────────────────────────
    #: The active Textual theme, PERSISTED. Chosen from the command palette
    #: (ctrl+p -> "Change theme") or /settings. Before this field the choice
    #: silently reset to textual-dark every boot. The 12 LiteSuite-ported
    #: themes (themes.py — matrix, lite-suite, ...) register alongside
    #: Textual's built-ins; an unknown name falls back to textual-dark.
    theme_name: str = "textual-dark"
    #: Custom themes made in the /settings creator: {name: {token: "#RRGGBB"}}
    #: with the 10 keys in themes.THEME_TOKENS. Registered at boot and on
    #: every settings save; a corrupt entry is skipped, never fatal.
    custom_themes: dict = field(default_factory=dict)
    show_thinking: bool = True
    #: Presentation-only terminal summary below the final assistant bubble.
    show_stop_line: bool = True
    #: Append the local wall-clock completion time to that summary.
    show_stop_time: bool = False
    autoscroll: bool = True
    #: Diagnostic copy in chat: plain by default, detail for original messages.
    error_message_style: str = "plain"
    #: How dialogs are presented. "modal" is the current behaviour and stays the
    #: default. "sidebar" mounts them in a `split: right` panel so the chat
    #: reflows narrower instead of being covered — see side_panel.py. This is a
    #: T075 SPIKE knob: only /test-sidebar honours it so far, and the existing
    #: dialogs are deliberately untouched until Ryan has looked at it.
    dialog_style: str = "modal"
    #: Which edge a sidebar dialog docks to: "right" (default, unchanged) or
    #: "left". Independent of dialog_style — this only matters when that is
    #: "sidebar", but it is a separate decision and gets a separate control
    #: rather than being folded into a four-state one.
    dialog_side: str = "right"
    #: Paste an image (Ctrl+O) and it auto-opens in the in-sidebar viewer,
    #: rendered as real pixels (default ON). OFF still ATTACHES the image to the
    #: model exactly as before and the manual view_image path is untouched — this
    #: toggles only the automatic preview, nothing else.
    image_viewer_enabled: bool = True

    # ── Footer ───────────────────────────────────────────────────────────────
    #: Each field of the status footer, individually. Defaults match what the
    #: footer showed before it was configurable, EXCEPT the percent — which was
    #: already being computed to choose the colour and then discarded.
    footer_show_seat: bool = True
    footer_show_thinking: bool = True
    #: Live background tasks and live subagents, as counts (T570). Hideable like
    #: every other footer field; ON by default because Ryan asked for them to be
    #: visible, and a count of zero renders as ABSENCE rather than "bg:0" so an
    #: idle session pays no width for them.
    footer_show_bg: bool = True
    footer_show_subagents: bool = True
    footer_show_convo: bool = True
    footer_show_context: bool = True
    footer_show_context_pct: bool = True
    footer_show_tps: bool = True
    #: Footer item ids in left-to-right order. Visibility remains controlled by
    #: the switches above; omitted/unknown ids are repaired on load/save.
    footer_order: list[str] = field(
        default_factory=lambda: list(FOOTER_ORDER_DEFAULT)
    )

    # ── Voice (TTS out) ───────────────────────────────────────────────────────
    #: Speak the agent's replies aloud (Ryan 2026-09-18). OFF by default — opt-in.
    #: Controlled from Voice settings and the input-border Speak button.
    tts_enabled: bool = False
    #: Maximum duration of a speech child, seconds.
    tts_timeout: int = 300
    #: "pyttsx3" (Windows SAPI direct, offline, no download — the default) or
    #: "edge" (Microsoft cloud neural voices; needs edge-tts + playsound).
    tts_engine: str = "pyttsx3"
    #: SAPI voice NAME for the pyttsx3 engine (e.g. "Microsoft Zira Desktop");
    #: blank = the system default voice.
    tts_voice: str = ""
    #: The voice id for the edge engine.
    tts_edge_voice: str = "en-GB-SoniaNeural"

    # ── Voice (STT in / dictation) ────────────────────────────────────────────
    #: faster-whisper model size: "tiny.en" | "base.en" | "small.en". Downloaded
    #: on first use (the opt-in). base.en is the ruling.
    stt_model: str = "base.en"
    #: Direct Show microphone NAME; blank = the first mic ffmpeg lists.
    stt_mic: str = ""
    #: The key that toggles voice recording (start/stop) from anywhere.
    #: Configurable in the Voice tab; bound at runtime (App.bind) so a change
    #: takes effect without a restart. Textual key spelling, e.g. "ctrl+space".
    stt_hotkey: str = "ctrl+space"

    #: Optional native sidecar is default-off; Textual screens remain fallback.
    sidecar_enabled: bool = False


#: field name → environment variable that overrides it.
#: Both pre-existing knobs are preserved by name so nothing that worked breaks.
ENV_OVERRIDES: dict[str, str] = {
    "lm_host": "LITETUI_LM_HOST",
    "ninfer_host": "LITETUI_NINFER_HOST",
    "ninfer_executable": "LITETUI_NINFER_EXE",
    "ninfer_artifact": "LITETUI_NINFER_ARTIFACT",
    "backend": "LITETUI_BACKEND",
    "llama_host": "LITETUI_LLAMA_HOST",
    "tool_iterations": "LM_TOOL_ITERS",
    "default_model": "LITETUI_MODEL",
    "max_tokens_tools": "LITETUI_MAX_TOKENS",
    "thinking_level": "LITETUI_THINKING",
    "seat_name": "LITETUI_SEAT_NAME",
    "tts_engine": "LITETUI_TTS_ENGINE",
    "stt_model": "LITETUI_STT_MODEL",
    "stt_hotkey": "LITETUI_STT_HOTKEY",
}


def settings_path(root: Path | None = None) -> Path:
    from litetui.paths import data_root

    return (root if root is not None else data_root()) / SETTINGS_FILENAME


def _coerce(name: str, raw: Any, current: Any) -> Any:
    """Coerce a JSON/env value to the field's declared type.

    Returns `current` on anything unparseable rather than raising: one bad line
    in settings.json must not stop the app from starting. A knob that reverts
    to its default is recoverable; an app that will not launch is not.
    """
    ftype = {f.name: f.type for f in fields(Settings)}.get(name)
    if ftype is None:
        return current
    t = str(ftype)
    try:
        if raw is None:
            return None if "None" in t else current
        if "bool" in t:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if "list[str]" in t:
            if isinstance(raw, list):
                return [str(x) for x in raw]
            return [s for s in str(raw).split(",") if s]
        if "dict" in t:
            # str(raw) below would turn a loaded {} into the STRING '{}' -
            # the same type-roundtrip class as the known skill_roots bug.
            return raw if isinstance(raw, dict) else current
        if "int" in t:
            return int(str(raw).strip())
        if "float" in t:
            return float(str(raw).strip())
        return str(raw)
    except (TypeError, ValueError):
        return current


def load(root: Path | None = None) -> Settings:
    """Defaults, then the settings file, then the environment."""
    s = Settings()
    p = settings_path(root)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict):
            known = {f.name for f in fields(Settings)}
            for k, v in data.items():
                if k in known:
                    setattr(s, k, _coerce(k, v, getattr(s, k)))
    object.__setattr__(s, '_saved_values', asdict(s))
    for name, env_key in ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is not None and raw != "":
            setattr(s, name, _coerce(name, raw, getattr(s, name)))
    s.tool_policy_profile = _selectable_profile(s.tool_policy_profile)
    if s.error_message_style not in ("plain", "detail"):
        s.error_message_style = "plain"
    s.footer_order = normalize_footer_order(s.footer_order)
    # The snapshot `save()` diffs against: everything this instance believes the
    # file said at load time. Not a field, so `asdict` never sees it.
    object.__setattr__(s, "_baseline", asdict(s))
    return s


def _selectable_profile(name: str) -> str:
    """T085 MIGRATION. A stored level that is no longer selectable lands on one.

    🔴 THIS IS NOT TIDINESS -- WITHOUT IT THE SETTINGS SCREEN CRASHES ON MOUNT.
    `scheduled` was an offered choice until T085 removed it from the cycle, so
    a settings.json written by yesterday's build can hold it. The screen builds
    its dropdown with `Select(choices, value=stored, allow_blank=False)`, and
    Textual raises `InvalidSelectValueError` from `Select._on_mount` when the
    stored value is not among the options. Measured, not assumed: mounting one
    with value="scheduled" against the T085 choices raises exactly that.

    That is the same defect class as a settings FIELD with no control -- which
    breaks Save from every tab -- arriving from the other side: a stored VALUE
    with no option. Both take out a screen the user opened to fix something
    else.

    ⚠️ THE LANDING IS THE NARROWEST SELECTABLE LEVEL, never the widest. Going
    `scheduled` -> `interactive` widens authority (read-only becomes
    ask-before-sensitive), which is unavoidable once the floor stops being
    selectable -- but every widened action now passes a human gate, and the
    alternative (`autonomous`) would silently grant everything to someone who
    had explicitly chosen read-only.
    """
    from litetui import tool_policy
    selectable = tool_policy.selectable_profile_names()
    if name in selectable:
        return name
    return selectable[0]


def _baseline(s: Settings) -> dict[str, object] | None:
    """What this instance last agreed the file said, or None if it never read one."""
    return getattr(s, "_baseline", None)


def save(s: Settings, root: Path | None = None) -> Path:
    """Write settings.json: only THIS instance's changes, and atomically.

    🔴 IT USED TO WRITE THE WHOLE DATACLASS, AND THAT ERASED THE OTHER INSTANCE.
    Ryan runs two LiteTUI processes from one repo (2026-09-12), both resolving
    `data_root()` to the same directory and therefore to ONE settings.json with
    seventy keys in it. Writing every field meant the second instance to save
    wrote back the snapshot it had loaded minutes earlier, undoing every change
    the first had made in between.
        AND IT IS NOT A RACE. No interleaving is needed: the two saves can be
    hours apart and the later one still wins every field it never touched.
    Reproduced in `tests/test_two_instances_coexist.py` — A sets the think level
    to xhigh, B later sets a theme, the file reads `medium`.

    ⇒ So a save now READS the file, applies only the keys that differ from this
    instance's baseline, and leaves the rest of the file exactly as it found it.

    Environment overrides are effective-only: an unrelated save does not
    persist them. An explicitly changed field is still a persistence request.

    🔴 ATOMIC, BECAUSE THE TORN FILE IS WORSE THAN THE LOST FIELD. `write_text`
    truncates before it writes, and `load()` answers an unparseable file with
    SILENT DEFAULTS ("one bad line must not stop the app") — so a reader landing
    between the two syscalls does not see an error, it sees a settings reset
    nobody is told about. Temp file plus one `os.replace`, the same shape
    `tasks.py` and `scheduler.py` already use; the target is never opened for
    writing at all.
    """
    from litetui.shared_state import coordinated_write

    p = settings_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with coordinated_write(p):
        return _save_locked(s, root)


def _save_locked(s: Settings, root: Path | None = None) -> Path:
    p = settings_path(root)
    current = asdict(s)
    base = _baseline(s)

    merged: dict[str, object] = {}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, dict):
            merged.update(existing)

    if base is None:
        # Never loaded: this Settings IS the intent, so it authors every key.
        merged.update(current)
    else:
        merged.update({k: v for k, v in current.items() if base.get(k) != v})

    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # The file and this instance now agree; a second save must not re-apply a
    # diff it has already written.
    object.__setattr__(s, "_baseline", dict(current))
    return p


def source_of(name: str) -> str | None:
    """The env var currently overriding `name`, or None.

    The settings screen renders these read-only. Offering an editable control
    for a value the environment will win back is a control that lies.
    """
    env_key = ENV_OVERRIDES.get(name)
    if env_key and os.environ.get(env_key):
        return env_key
    return None


def sampling_kwargs(s: Settings) -> dict[str, Any]:
    """The optional LM Studio sampling flags that are actually set.

    Only non-None values appear. Sending `temperature: null` or a zero the user
    never chose changes generation; omitting the key leaves the server's own
    default in charge, which is what "unset" has to mean.
    """
    out: dict[str, Any] = {}
    for name in (
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "repeat_penalty",
        "presence_penalty",
        "frequency_penalty",
        "seed",
    ):
        v = getattr(s, name)
        if v is not None:
            out[name] = v
    if s.stop:
        out["stop"] = list(s.stop)
    return out


# "off" is this app's name for the server's "none" so the wording matches
# the rest of the UI; everything else passes through unchanged. Lives here
# because two plugins (misc's /think, help's text) and the app's wire path
# all read it — and plugins must never import app (it runs as __main__).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")
