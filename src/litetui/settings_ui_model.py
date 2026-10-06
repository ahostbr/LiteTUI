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
    description: str = ""


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


# Human-facing copy is owned here so Textual and the sidecar can use the same words.
# The technical name stays in SettingFieldSpec.name for search and support.
FIELD_LABELS = {
    "backend": "How to run your model", "default_model": "Your usual model",
    "pin_default_model": "Keep the usual model selected", "default_context_length": "Conversation memory size",
    "backend_chosen": "Remember the engine choice", "codex_native_engine": "Use Codex's own agent loop",
    "thinking_level": "How deeply to think", "show_thinking": "Show thinking as it happens",
    "show_stop_line": "Show when a reply finishes", "show_stop_time": "Show when a reply finished",
    "autoscroll": "Follow new replies", "error_message_style": "How errors are explained",
    "theme_name": "Your color theme", "custom_themes": "Themes you made",
    "sidecar_enabled": "Open settings in this window", "dialog_style": "How dialogs open",
    "dialog_side": "Which side dialogs use", "image_viewer_enabled": "Preview images",
    "tts_enabled": "Read replies aloud", "tts_engine": "Fallback speech provider", "tts_voice": "Speaking voice",
    "tts_litesuite_first": "Prefer LiteSuite speech",
    "stt_model": "Transcription model", "stt_mic": "Microphone", "stt_hotkey": "Dictation shortcut",
    "tools_enabled": "Let the agent use tools", "tool_iterations": "Maximum tool steps",
    "tool_policy_profile": "Approval level", "tool_always_allow": "Actions always allowed",
    "tool_deny": "Actions never allowed", "tools_disabled": "Hidden tools",
    "tool_trusted_interpreters": "Trusted interpreter paths",
    "autocompact_enabled": "Keep long conversations going", "autocompact_at_percent": "When to shorten context",
    "wake_after_compact": "Resume after shortening", "mcp_enabled": "Connect external tools",
    "skills_enabled": "Enable skills", "footer_order": "Footer item order",
    "max_tokens_tools": "Agent reply length", "max_tokens_chat": "Chat reply length",
    "temperature": "How varied replies feel", "seed": "Repeatable generation seed",
    "groq_api_key": "Groq key", "cerebras_api_key": "Cerebras key", "nvidia_api_key": "NVIDIA key",
    "mistral_api_key": "Mistral key", "github_models_token": "GitHub Models key",
    "openrouter_api_key": "OpenRouter key", "gemini_api_key": "Gemini key",
    "ollama_api_key": "Ollama key", "zai_api_key": "Z.ai key",
    "cloudflare_api_key": "Cloudflare key", "longcat_api_key": "LongCat key",
    "sealion_api_key": "SEA-LION key",
}


def _friendly_label(name: str) -> str:
    if name in FIELD_LABELS:
        return FIELD_LABELS[name]
    words = name.replace("_", " ").split()
    special = {"tts": "Speech", "stt": "Dictation", "mcp": "MCP", "lmstudio": "LM Studio",
               "lm": "Local model", "ninfer": "NInfer", "llama": "llama.cpp", "api": "API",
               "hf": "Hugging Face", "kv": "KV", "tps": "tokens per second", "pct": "percentage",
               "s": "seconds", "mib": "MiB", "gguf": "GGUF"}
    return " ".join(special.get(word, word) for word in words).capitalize()


# The one-line effect of each control. Neither section copy nor a key-to-title
# transform can say what a numeric budget costs or what a permission changes.
FIELD_DESCRIPTIONS = {
    "subagent_route": "One child backend and exact model shared by all instances; conversation overrides win. Follow parent clears the global route.",
    "subagent_route_override": "Conversation child route: inherit the global route, follow the parent, or select a backend and model. Clear the legacy subagent model to inherit.",
    "allow_local_subagents": "Expert only: local children can exhaust GPU memory. Never loads models; requires the sole resident model and nothing loading. LM Studio remains unsupported until its usage/lease protocol lands.",
    "default_model": "The model selected when a conversation connects to its engine.",
    "pin_default_model": "Always switch back to your usual model on connect; off only picks it when none is loaded.",
    "default_context_length": "Tokens loaded for the context window; a larger window can cost VRAM and speed.",
    "lm_host": "The address and port where LiteTUI reaches LM Studio.",
    "lmstudio_graded_thinking_models": "Exact LM Studio model IDs allowed graded reasoning levels instead of on/off.",
    "backend": "The engine responsible for generating replies in this conversation.",
    "codex_native_engine": "Let Codex manage the agent loop instead of using LiteTUI's loop.",
    "groq_api_key": "Save a Groq key for requests to its models; the key stays hidden here.",
    "cerebras_api_key": "Save a Cerebras key for requests to its models; the key stays hidden here.",
    "nvidia_api_key": "Save an NVIDIA key for requests to its models; the key stays hidden here.",
    "mistral_api_key": "Save a Mistral key for requests to its models; the key stays hidden here.",
    "github_models_token": "Save a GitHub Models token for model requests; the token stays hidden here.",
    "openrouter_api_key": "Save an OpenRouter key for model requests; the key stays hidden here.",
    "gemini_api_key": "Save a Gemini key for model requests; the key stays hidden here.",
    "ollama_api_key": "Save an Ollama key if that server requires authentication; the key stays hidden here.",
    "zai_api_key": "Save a Z.ai key for model requests; the key stays hidden here.",
    "cloudflare_api_key": "Save a Cloudflare key for model requests; the key stays hidden here.",
    "longcat_api_key": "Save a LongCat key for model requests; the key stays hidden here.",
    "sealion_api_key": "Save a SEA-LION key for model requests; the key stays hidden here.",
    "llama_executable": "Executable used when LiteTUI starts its llama.cpp router.",
    "llama_host": "Address used to reach the llama.cpp router.",
    "llama_attach_hosts": "Existing llama.cpp server addresses tried in order before starting a router.",
    "llama_scan_litesuite": "Include LiteSuite's models when looking for local GGUF files.",
    "llama_scan_lmstudio": "Include LM Studio's models when looking for local GGUF files.",
    "llama_scan_hf_cache": "Include the Hugging Face cache when looking for local GGUF files.",
    "llama_models_dirs": "Extra folders to search for GGUF model files.",
    "llama_models_max": "Maximum llama.cpp models kept loaded at once; more models need more memory.",
    "lms_load_timeout_s": "Seconds to wait for an LM Studio load before giving up; large models may need longer.",
    "ninfer_host": "Address of an NInfer server started separately; blank discovers LiteSuite's engine.",
    "ninfer_executable": "NInfer server executable used by the next /engine start.",
    "ninfer_artifact": "Model artifact served at the next NInfer engine start.",
    "ninfer_max_context": "Context tokens requested at NInfer startup; larger windows use more GPU memory.",
    "ninfer_max_concurrency": "Requests decoded together by NInfer; parallel requests share its context pool.",
    "ninfer_kv_dtype": "NInfer cache format; higher precision needs more memory, smaller formats save it.",
    "ninfer_kv_capacity": "Shared NInfer cache capacity: blank follows context, auto fills remaining GPU memory.",
    "ninfer_host_kv_mib": "MiB of pinned system RAM for NInfer cache offloaded from the GPU.",
    "tts_enabled": "Show Speak on replies; turning it off stops LiteTUI-owned playback, not LiteSuite's queue.",
    "tts_litesuite_first": "Use LiteSuite's selected voice when reachable; off uses only the fallback engine. LiteSuite owns queued playback and stopping it.",
    "tts_engine": "Fallback when LiteSuite is unavailable, or the primary engine when LiteSuite preference is off.",
    "tts_voice": "Windows voice used for spoken replies with the local speech engine.",
    "tts_timeout": "Maximum lifetime in seconds of a LiteTUI speech child; does not control LiteSuite's queue.",
    "tts_edge_voice": "Neural voice used for spoken replies with the Edge speech engine.",
    "stt_model": "Transcription model that turns recorded speech into prompt text.",
    "stt_mic": "Microphone used to record dictation.",
    "stt_hotkey": "Shortcut that begins dictation from the keyboard.",
    "thinking_level": "Reasoning effort requested from models that support this level.",
    "max_tokens_tools": "Token budget for an agent turn, including reasoning; too little can leave no reply.",
    "max_tokens_chat": "Token budget for a plain chat reply without tool use.",
    "temperature": "Sampling randomness; higher values generally make replies more varied.",
    "top_p": "Keep only tokens inside this cumulative probability for sampling.",
    "top_k": "Sample from at most this many top-ranked next tokens.",
    "min_p": "Discard candidate tokens below this relative probability threshold.",
    "repeat_penalty": "Discourage the model from repeating tokens it has already generated.",
    "presence_penalty": "Discourage reusing tokens that have appeared at least once.",
    "frequency_penalty": "Discourage reused tokens more as their occurrence count rises.",
    "seed": "Fixed generation seed for repeatable runs; blank leaves each turn random.",
    "stop": "Text sequences that end generation when encountered.",
    "subagent_model": "Loaded model assigned to parallel subagent calls; nothing loads automatically.",
    "tool_summary_model": "Model used for tool-output summaries; an unloaded pick falls back to the main model.",
    "tools_enabled": "Permit tools in this conversation; off leaves the agent in plain chat.",
    "tool_iterations": "Maximum tool-use rounds in a turn before the agent is stopped.",
    "tool_auto_background_s": "Seconds before an eligible running shell call moves to background; zero disables it.",
    "enter_interrupts": "Make Enter interrupt a running reply instead of queuing your message.",
    "tool_policy_profile": "Host-enforced tool permissions for typed turns, inbox work, and scheduled jobs.",
    "tool_always_allow": "Tool:authority approvals that skip future prompts at that authority; use sparingly.",
    "tool_deny": "Tool:authority refusals checked before any allow rule or profile.",
    "tool_trusted_interpreters": "One exact absolute path per line for this conversation. Empty adds no trust. Skips only foreign-program confirmation, not danger or deny rules; linked or invalid paths grant no trust.",
    "relay_approval_timeout_s": "Seconds an agent-launched seat waits for its spawner's approval before refusing.",
    "tools_disabled": "Tool names withheld from the model and refused if called anyway.",
    "tool_context_mode": "Put raw, masked, or summarized tool output in context without deleting the original.",
    "tool_context_threshold_chars": "Results shorter than this character count enter context unchanged.",
    "autocompact_enabled": "Automatically summarize history as the context window fills.",
    "autocompact_at_percent": "Percent of context used before compaction starts; leave room to write its summary.",
    "wake_after_compact": "Resume an in-flight agent loop after its conversation is compacted.",
    "compact_max_tokens": "Token budget for the summary; too little may be spent entirely on reasoning.",
    "compact_thinking_level": "Reasoning effort used when generating a compaction summary.",
    "compact_max_tool_iters": "Maximum tool round-trips allowed while compaction writes its summary.",
    "clear_screen_after_compact": "Clear replaced transcript messages so the screen matches the model's context.",
    "compact_keep_recent": "Number of recent messages kept verbatim alongside the summary.",
    "user_name": "Name included in the assistant's system prompt; blank omits it.",
    "seat_name": "Name this seat asks the fleet registry to show in the footer and discover list.",
    "skills_enabled": "Offer indexed skills to the agent and load their full bodies only on request.",
    "skill_roots": "Additional folders scanned for skill indexes after the built-in library.",
    "mcp_enabled": "Start configured MCP servers so their external tools are available.",
    "theme_name": "Apply a built-in or saved palette to the interface.",
    "show_thinking": "Display model thinking while the reply is in progress.",
    "show_stop_line": "Place a visible divider where a reply finishes.",
    "show_stop_time": "Show the completion time beside a finished reply.",
    "autoscroll": "Keep the transcript following new replies as they arrive.",
    "error_message_style": "Show errors in plain language or with fuller technical detail.",
    "dialog_style": "Open interrupting dialogs in a sidebar or over the conversation.",
    "dialog_side": "Put sidebar dialogs on the left or right of the conversation.",
    "image_viewer_enabled": "Preview attached images in the interface.",
    "sidecar_enabled": "Prefer the optional native settings window over the Textual settings screen.",
    "footer_show_seat": "Display the fleet seat name in the footer.",
    "footer_show_thinking": "Display the current reasoning effort in the footer.",
    "footer_show_bg": "Display active background process count in the footer.",
    "footer_show_subagents": "Display active subagent count in the footer.",
    "footer_show_convo": "Display the first eight characters of the conversation ID in the footer.",
    "footer_show_context": "Display used and available context tokens in the footer.",
    "footer_show_context_pct": "Display the context window's used percentage in the footer.",
    "footer_show_tps": "Display last turn's generation speed in tokens per second.",
    "footer_show_cache": "Display prompt-cache warmth and hit rate, where the backend supplies them.",
    "footer_task_manager": "Show live CPU, memory, GPU, disk, and network meters in the footer.",
    "footer_show_key_hints": "Show keyboard-shortcut hints in the footer.",
    "footer_order": "Arrange visible footer facts left to right by their IDs.",
    "backend_chosen": "Remember that the engine was explicitly picked rather than inferred by default.",
    "claude_executable": "Executable used when LiteTUI launches the Claude backend.",
    "custom_api_key_env": "Environment variable name holding the custom provider's API key, not the key itself.",
    "custom_base_url": "Base URL for requests to a custom model provider.",
    "custom_context_length": "Context token limit advertised for the custom provider's model.",
    "llama_load_settings": "Saved loading parameters applied when opening llama.cpp models.",
    "llama_presets": "Named llama.cpp configurations available for later model loads.",
    "model_infer_overrides": "Inference parameters saved for individual models instead of all models.",
    "mcp_disabled_servers": "Configured MCP server names prevented from starting.",
    "plugins_disabled": "Plugin names skipped when LiteTUI loads extensions.",
    "user_name_asked": "Remember that the first-run name question was already shown.",
    "custom_themes": "Custom color palettes stored for reuse in the theme picker.",
}


def _field_description(name: str) -> str:
    return FIELD_DESCRIPTIONS[name]


def _fields(
    names: tuple[str, ...],
    *,
    scope: SettingScope,
    labels: dict[str, str] | None = None,
    keywords: dict[str, tuple[str, ...]] | None = None,
    summary: str = "",
) -> tuple[SettingFieldSpec, ...]:
    labels = labels or {}
    keywords = keywords or {}
    return tuple(
        SettingFieldSpec(
            name=name,
            label=labels.get(name, _friendly_label(name)),
            scope=scope,
            keywords=keywords.get(name, ()),
            description=_field_description(name),
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
        fields=_fields(names, scope=scope, labels=labels, keywords=field_keywords, summary=summary),
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
        "model", "Model", "model-free-keys", "Free-tier keys",
        "One key per keyed free source; a saved key wins over its environment variable.",
        ("groq_api_key", "cerebras_api_key", "nvidia_api_key", "mistral_api_key",
         "github_models_token", "openrouter_api_key", "gemini_api_key", "ollama_api_key", "zai_api_key",
         "cloudflare_api_key", "longcat_api_key", "sealion_api_key"),
        scope="device", keywords=("free", "key", "Groq", "Cerebras", "NVIDIA", "Mistral", "GitHub", "OpenRouter", "Gemini",
                                  "Ollama", "Z.ai", "GLM", "Cloudflare", "LongCat", "SEA-LION"),
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
        ("ninfer_max_context", "ninfer_max_concurrency", "ninfer_kv_dtype", "ninfer_kv_capacity", "ninfer_host_kv_mib"), scope="device", keywords=("context", "parallel", "concurrency", "KV"),
    ),
    _section(
        "voice", "Voice", "voice-speak", "Speak / TTS out",
        "Reply playback, voice choice, and the install/test path.",
        ("tts_enabled", "tts_litesuite_first", "tts_engine", "tts_voice", "tts_timeout", "tts_edge_voice"), scope="device", keywords=("speak", "TTS", "voice", "LiteSuite", "Microsoft", "offline", "timeout"),
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
        "agent", "Agent loop", "agent-subagent-routing", "Subagents · global route",
        "One backend and model shared by all instances; conversation overrides win.",
        ("subagent_route", "allow_local_subagents"), scope="device", keywords=("Codex", "Claude", "global", "subagent", "child", "local"),
    ),
    _section(
        "agent", "Agent loop", "agent-routing", "Side-call routing",
        "Point background work at the right resident model.",
        ("subagent_route_override", "subagent_model", "tool_summary_model"), scope="conversation", keywords=("subagent", "side call", "summary", "child"),
    ),
    _section(
        "agent", "Agent loop", "agent-execution", "Execution limits",
        "How far a turn can run before it yields or moves work aside.",
        ("tools_enabled", "tool_iterations", "tool_auto_background_s", "enter_interrupts"), scope="conversation", keywords=("tools", "iterations", "background", "interrupt", "turn"),
    ),
    _section(
        "agent", "Agent loop", "agent-authority", "Authority & approvals",
        "The host-enforced safety profile and standing decisions.",
        ("tool_policy_profile", "tool_always_allow", "tool_deny", "tool_trusted_interpreters", "relay_approval_timeout_s"), scope="conversation", keywords=("authority", "approval", "permissions", "safety", "allow", "deny"),
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
        ("user_name", "seat_name"), scope="defaults", keywords=("user", "name", "fleet", "seat", "identity", "registry"),
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
        ("dialog_style", "dialog_side", "image_viewer_enabled", "sidecar_enabled"), scope="device", keywords=("dialog", "sidebar", "media", "image", "paste", "sidecar", "native"), field_keywords={"dialog_style": ("sidebar", "modal"), "dialog_side": ("sidebar", "left", "right"), "image_viewer_enabled": ("paste", "preview"), "sidecar_enabled": ("native", "optional", "window")},
    ),
    _section(
        "interface", "Interface", "interface-footer", "Footer telemetry",
        "Choose which runtime facts stay in the footer, then arrange them left to right.",
        ("footer_show_seat", "footer_show_thinking", "footer_show_bg", "footer_show_subagents", "footer_show_convo", "footer_show_context", "footer_show_context_pct", "footer_show_tps", "footer_show_cache", "footer_task_manager", "footer_show_key_hints", "footer_order"), scope="app", keywords=("footer", "telemetry", "left to right", "ordering", "status"), field_keywords={"footer_order": ("ordering", "left to right", "position",)}, default_expanded=True,
    ),
)


# Every public setting has a place in the same information architecture.
# These are current Settings fields that the earlier planned IA omitted.
_EXTRA_FIELDS = (
    ("model", "Model", "model-extra", "Connections & saved engines", "Choose where LiteTUI connects and what it remembers.",
     ("backend_chosen", "claude_executable", "custom_api_key_env", "custom_base_url", "custom_context_length", "llama_load_settings", "llama_presets", "model_infer_overrides")),
    ("capabilities", "Capabilities", "cap-extra", "Connected extensions", "Choose which extensions and servers can run.",
     ("mcp_disabled_servers", "plugins_disabled", "user_name_asked")),
    ("themes", "Themes", "theme-saved", "Saved themes", "Your custom palettes live here.", ("custom_themes",)),
)
SETTINGS_SECTIONS += tuple(
    _section(tab_id, tab_label, section_id, title, summary, names, scope="mixed")
    for tab_id, tab_label, section_id, title, summary, names in _EXTRA_FIELDS
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
        " ".join((field.name, field.label, field.description, *field.keywords))
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
            if all(term in " ".join((field.name, field.label, field.description, *field.keywords)).lower() for term in terms)
        )
        scopes = tuple(dict.fromkeys(field.scope for field in section.fields))
        hits.append(SettingSearchHit(section.tab_id, section.tab_label, section.section_id, section.title, matching_fields, scopes))
    return tuple(hits)
