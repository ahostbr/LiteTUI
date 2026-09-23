"""Textual-independent information architecture for the settings surface.

This module deliberately describes scope and discoverability, not persistence.
The conversation-owned mapping is a reviewable contract for the future
ConvoSettings adapter; it must not be interpreted as permission to write every
``Settings`` field globally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SettingScope = Literal["conversation", "defaults", "device", "app", "mixed"]


@dataclass(frozen=True)
class SettingFieldSpec:
    name: str
    label: str
    scope: SettingScope
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class SettingsTabSpec:
    tab_id: str
    label: str
    section_ids: tuple[str, ...]


@dataclass(frozen=True)
class SettingsSectionSpec:
    tab_id: str
    tab_label: str
    section_id: str
    title: str
    summary: str
    fields: tuple[SettingFieldSpec, ...]
    keywords: tuple[str, ...] = ()
    default_expanded: bool = False


@dataclass(frozen=True)
class SettingSearchHit:
    tab_id: str
    tab_label: str
    section_id: str
    section_title: str
    field_names: tuple[str, ...]
    scopes: tuple[SettingScope, ...]


def _fields(
    names: tuple[str, ...],
    *,
    scope: SettingScope,
    labels: dict[str, str] | None = None,
    keywords: dict[str, tuple[str, ...]] | None = None,
) -> tuple[SettingFieldSpec, ...]:
    labels = labels or {}
    keywords = keywords or {}
    return tuple(
        SettingFieldSpec(
            name=name,
            label=labels.get(name, name.replace("_", " ")),
            scope=scope,
            keywords=keywords.get(name, ()),
        )
        for name in names
    )


def _section(
    tab_id: str,
    tab_label: str,
    section_id: str,
    title: str,
    summary: str,
    names: tuple[str, ...],
    *,
    scope: SettingScope,
    keywords: tuple[str, ...] = (),
    labels: dict[str, str] | None = None,
    field_keywords: dict[str, tuple[str, ...]] | None = None,
    default_expanded: bool = False,
) -> SettingsSectionSpec:
    return SettingsSectionSpec(
        tab_id=tab_id,
        tab_label=tab_label,
        section_id=section_id,
        title=title,
        summary=summary,
        fields=_fields(names, scope=scope, labels=labels, keywords=field_keywords),
        keywords=keywords,
        default_expanded=default_expanded,
    )


SETTINGS_SECTIONS: tuple[SettingsSectionSpec, ...] = (
    _section(
        "model", "Model", "model-connection", "Connection & default model",
        "The first decisions to make when a conversation opens.",
        ("default_model", "pin_default_model", "default_context_length", "lm_host", "lmstudio_graded_thinking_models"),
        scope="conversation", keywords=("connect", "model", "context", "LM Studio", "reasoning"),
        default_expanded=True,
    ),
    _section(
        "model", "Model", "model-engine", "Engine selection",
        "Which backend owns inference and whether Codex owns the loop.",
        ("backend", "codex_native_engine"), scope="conversation", keywords=("backend", "engine", "Codex", "loop"),
    ),
    _section(
        "model", "Model", "model-router", "llama.cpp router",
        "The local router executable, address, and safe attach order.",
        ("llama_executable", "llama_host", "llama_attach_hosts"), scope="device", keywords=("router", "server", "attach", "address"),
    ),
    _section(
        "model", "Model", "model-discovery", "Model discovery",
        "Where GGUF models may be found before they enter the picker.",
        ("llama_scan_litesuite", "llama_scan_lmstudio", "llama_scan_hf_cache", "llama_models_dirs"), scope="device", keywords=("scan", "GGUF", "HuggingFace", "cache", "folders"),
    ),
    _section(
        "model", "Model", "model-loading", "Load lifecycle",
        "Resident-model budget and the control-plane timeout.",
        ("llama_models_max", "lms_load_timeout_s"), scope="device", keywords=("resident", "timeout", "load", "VRAM"),
    ),
    _section(
        "ninfer", "NInfer", "ninfer-attach", "Attachment",
        "Connect to the engine LiteSuite started, or name a hand-started one.",
        ("ninfer_host", "ninfer_executable", "ninfer_artifact"), scope="device", keywords=("NVIDIA", "RTX 5090", "engine", "artifact"),
    ),
    _section(
        "ninfer", "NInfer", "ninfer-envelope", "Runtime envelope",
        "The startup flags that shape memory and parallel work.",
        ("ninfer_max_context", "ninfer_max_concurrency"), scope="device", keywords=("context", "parallel", "concurrency", "KV"),
    ),
    _section(
        "voice", "Voice", "voice-speak", "Speak / TTS out",
        "Reply playback, voice choice, and the install/test path.",
        ("tts_enabled", "tts_engine", "tts_voice", "tts_timeout", "tts_edge_voice"), scope="device", keywords=("speak", "TTS", "voice", "Microsoft", "offline", "timeout"),
    ),
    _section(
        "voice", "Voice", "voice-dictate", "Dictate / STT in",
        "Capture, transcribe, and place the result in the prompt input.",
        ("stt_model", "stt_mic", "stt_hotkey"), scope="device", keywords=("dictate", "STT", "microphone", "record", "Whisper"),
    ),
    _section(
        "generation", "Generation", "generation-reasoning", "Reasoning",
        "The level the loaded model can actually honor.",
        ("thinking_level",), scope="conversation", keywords=("thinking", "reasoning", "effort"), default_expanded=True,
    ),
    _section(
        "generation", "Generation", "generation-budget", "Output budgets",
        "Response ceilings for agent turns versus plain chat.",
        ("max_tokens_tools", "max_tokens_chat"), scope="defaults", keywords=("tokens", "budget", "output", "response"),
    ),
    _section(
        "generation", "Generation", "generation-sampling", "Sampling",
        "The controls that change how likely tokens are chosen.",
        ("temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty", "frequency_penalty"), scope="defaults", keywords=("sampling", "randomness", "probability", "penalty"),
    ),
    _section(
        "generation", "Generation", "generation-repro", "Reproducibility",
        "Make a run repeatable—or explicitly leave it random.",
        ("seed", "stop"), scope="defaults", keywords=("seed", "repeat", "stop strings", "deterministic"),
    ),
    _section(
        "agent", "Agent loop", "agent-routing", "Side-call routing",
        "Point background work at the right resident model.",
        ("subagent_model", "tool_summary_model"), scope="conversation", keywords=("subagent", "side call", "summary", "child"),
    ),
    _section(
        "agent", "Agent loop", "agent-execution", "Execution limits",
        "How far a turn can run before it yields or moves work aside.",
        ("tools_enabled", "tool_iterations", "tool_auto_background_s", "enter_interrupts"), scope="conversation", keywords=("tools", "iterations", "background", "interrupt", "turn"),
    ),
    _section(
        "agent", "Agent loop", "agent-authority", "Authority & approvals",
        "The host-enforced safety profile and standing decisions.",
        ("tool_policy_profile", "tool_always_allow", "tool_deny"), scope="conversation", keywords=("authority", "approval", "permissions", "safety", "allow", "deny"),
    ),
    _section(
        "agent", "Agent loop", "agent-tools", "Tool surface",
        "Keep the model's advertised tools aligned with runtime policy.",
        ("tools_disabled",), scope="conversation", keywords=("tools", "disabled", "schemas", "registry"),
    ),
    _section(
        "agent", "Agent loop", "agent-context", "Tool-result context",
        "Choose raw, masked, or summarized tool output without destroying the sidecar.",
        ("tool_context_mode", "tool_context_threshold_chars"), scope="conversation", keywords=("tool output", "context", "mask", "summary"),
    ),
    _section(
        "compaction", "Compaction", "compact-trigger", "Trigger & resume",
        "When compaction starts, and whether an in-flight loop wakes again.",
        ("autocompact_enabled", "autocompact_at_percent", "wake_after_compact"), scope="conversation", keywords=("compact", "threshold", "resume", "wake"),
    ),
    _section(
        "compaction", "Compaction", "compact-summary", "Summary budget",
        "Give the summary enough room to be useful and durable.",
        ("compact_max_tokens", "compact_thinking_level", "compact_max_tool_iters"), scope="conversation", keywords=("summary", "budget", "thinking", "tool rounds"),
    ),
    _section(
        "compaction", "Compaction", "compact-transcript", "Transcript presentation",
        "Keep what the user can scroll back to aligned with what the model sees.",
        ("clear_screen_after_compact", "compact_keep_recent"), scope="app", keywords=("transcript", "screen", "recent", "history"),
    ),
    _section(
        "capabilities", "Capabilities", "cap-identity", "Fleet identity",
        "The name the harness registry sees.",
        ("seat_name",), scope="conversation", keywords=("fleet", "seat", "identity", "registry"),
    ),
    _section(
        "capabilities", "Capabilities", "cap-skills", "Skills",
        "Load the index cheaply; fetch skill bodies only when needed.",
        ("skills_enabled", "skill_roots"), scope="device", keywords=("skills", "libraries", "roots", "Claude"),
    ),
    _section(
        "capabilities", "Capabilities", "cap-mcp", "MCP servers",
        "Start declared servers, then selectively disable individual names.",
        ("mcp_enabled",), scope="device", keywords=("MCP", "servers", "plugins", "external"),
    ),
    _section(
        "hooks", "Hooks", "hooks-scope", "Scope & inventory",
        "Expose the effective global/project chain before editing.",
        (), scope="device", keywords=("hooks", "events", "executable", "repair", "test"), default_expanded=True,
    ),
    _section(
        "hooks", "Hooks", "hooks-policy", "Lifecycle policy",
        "Enable, gate, and scope a hook without deleting its declaration.",
        (), scope="device", keywords=("hooks", "policy", "enabled", "observe", "gate"),
    ),
    _section(
        "hooks", "Hooks", "hooks-command", "Command contract",
        "Keep the hook ID, executable, arguments, and working directory together.",
        (), scope="device", keywords=("hooks", "command", "executable", "arguments", "cwd"),
    ),
    _section(
        "hooks", "Hooks", "hooks-runtime", "Runtime envelope",
        "Tune environment, timeout, and ordering for matching hooks.",
        (), scope="device", keywords=("hooks", "runtime", "environment", "timeout", "order"),
    ),
    _section(
        "hooks", "Hooks", "hooks-test", "Test & recovery",
        "Repair malformed JSON, save/reload, and run a sample event explicitly.",
        (), scope="device", keywords=("hooks", "test", "repair", "recovery", "reload"), default_expanded=True,
    ),
    _section(
        "themes", "Themes", "theme-active", "Active theme",
        "A quick selection surface for built-ins, ports, shades, and customs.",
        ("theme_name",), scope="app", keywords=("theme", "palette", "colors", "visual"), default_expanded=True,
    ),
    _section(
        "themes", "Themes", "theme-custom", "Custom theme identity",
        "Name the palette and make save behavior obvious.",
        (), scope="app", keywords=("custom", "name", "create", "edit"),
    ),
    _section(
        "themes", "Themes", "theme-tokens", "Palette tokens",
        "Edit the semantic, surface, thinking, tool, and footer vocabulary.",
        (), scope="app", keywords=("tokens", "foreground", "background", "footer", "thinking", "tool"),
    ),
    _section(
        "interface", "Interface", "interface-transcript", "Transcript & motion",
        "What stays visible while the model works.",
        ("show_thinking", "show_stop_line", "show_stop_time", "autoscroll", "error_message_style"), scope="app", keywords=("thinking", "stop", "completion", "follow", "output", "errors", "Plain Talk", "Full Detail"), default_expanded=True,
    ),
    _section(
        "interface", "Interface", "interface-dialogs", "Dialogs & media",
        "Choose whether interruptions cover the chat and how pasted images appear.",
        ("dialog_style", "dialog_side", "image_viewer_enabled"), scope="device", keywords=("dialog", "sidebar", "media", "image", "paste"), field_keywords={"dialog_style": ("sidebar", "modal"), "dialog_side": ("sidebar", "left", "right"), "image_viewer_enabled": ("paste", "preview")},
    ),
    _section(
        "interface", "Interface", "interface-footer", "Footer telemetry",
        "Choose which runtime facts stay in the footer, then arrange them left to right.",
        ("footer_show_seat", "footer_show_thinking", "footer_show_bg", "footer_show_subagents", "footer_show_convo", "footer_show_context", "footer_show_context_pct", "footer_show_tps", "footer_order"), scope="app", keywords=("footer", "telemetry", "left to right", "ordering", "status"), field_keywords={"footer_order": ("ordering", "left to right", "position",)}, default_expanded=True,
    ),
)


SETTINGS_TABS: tuple[SettingsTabSpec, ...] = tuple(
    SettingsTabSpec(
        tab_id=tab_id,
        label=next(section.tab_label for section in SETTINGS_SECTIONS if section.tab_id == tab_id),
        section_ids=tuple(section.section_id for section in SETTINGS_SECTIONS if section.tab_id == tab_id),
    )
    for tab_id in dict.fromkeys(section.tab_id for section in SETTINGS_SECTIONS)
)


def settings_sections_for_tab(tab_id: str) -> tuple[SettingsSectionSpec, ...]:
    return tuple(section for section in SETTINGS_SECTIONS if section.tab_id == tab_id)


def _search_text(section: SettingsSectionSpec) -> str:
    fields = " ".join(
        " ".join((field.name, field.label, *field.keywords))
        for field in section.fields
    )
    return " ".join((section.tab_label, section.title, section.summary, *section.keywords, fields)).lower()


def search_settings(query: str, active_tab: str | None = None) -> tuple[SettingSearchHit, ...]:
    """Find sections/fields without importing Textual or a persistence service."""
    terms = tuple(part for part in query.lower().split() if part)
    if not terms:
        return ()
    hits: list[SettingSearchHit] = []
    for section in SETTINGS_SECTIONS:
        if active_tab and section.tab_id != active_tab:
            continue
        haystack = _search_text(section)
        if not all(term in haystack for term in terms):
            continue
        matching_fields = tuple(
            field.name
            for field in section.fields
            if all(term in " ".join((field.name, field.label, *field.keywords)).lower() for term in terms)
        )
        scopes = tuple(dict.fromkeys(field.scope for field in section.fields))
        hits.append(SettingSearchHit(section.tab_id, section.tab_label, section.section_id, section.title, matching_fields, scopes))
    return tuple(hits)
