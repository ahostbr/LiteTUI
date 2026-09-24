"""Scope of exposed settings when Codex owns the inference loop."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Control:
    owner: str
    help: str

    @property
    def editable(self):
        return self.owner in {"native", "host"}


ENGINE = Control(
    "engine",
    "Managed by the official Codex engine. This saved local-backend value does not apply to Codex.",
)
SAMPLING = Control(
    "unsupported",
    "The Codex app-server does not expose this sampling control. Saved for local backends.",
)
HOST_OUTPUT = Control(
    "host",
    "Applies to LiteTUI host-tool results. Codex built-in tool results remain managed by its engine.",
)
LOCAL_SERVER = Control(
    "unsupported",
    "Configures a local model server, not the Codex app-server. Saved values are preserved for local backends.",
)
CONTROLS = {
    **dict.fromkeys(
        (
            "default_context_length", "lm_host", "lms_load_timeout_s",
            "lmstudio_graded_thinking_models", "llama_executable", "llama_host",
            "llama_attach_hosts", "llama_scan_litesuite", "llama_scan_lmstudio",
            "llama_scan_hf_cache", "llama_models_dirs", "llama_models_max",
            "custom_base_url", "custom_api_key_env", "custom_context_length",
        ),
        LOCAL_SERVER,
    ),
    **dict.fromkeys(
        (
            "temperature",
            "top_p",
            "top_k",
            "min_p",
            "repeat_penalty",
            "presence_penalty",
            "frequency_penalty",
            "seed",
            "stop",
        ),
        SAMPLING,
    ),
    **dict.fromkeys(
        (
            "max_tokens_tools",
            "max_tokens_chat",
            "tool_iterations",
            "autocompact_enabled",
            "autocompact_at_percent",
            "compact_max_tokens",
            "compact_thinking_level",
            "compact_max_tool_iters",
            "compact_keep_recent",
            "clear_screen_after_compact",
            "wake_after_compact",
        ),
        ENGINE,
    ),
    **dict.fromkeys(
        ("tool_context_mode", "tool_context_threshold_chars", "tool_summary_model"),
        HOST_OUTPUT,
    ),
    "thinking_level": Control(
        "native",
        "Default for new conversations; sent to the official Codex engine. Existing conversations retain their chosen mode. Max and Ultra use the installed engine's mode definitions.",
    ),
    "tool_auto_background_s": Control(
        "host",
        "Applies to LiteTUI bash/powershell tools. Native Codex commands use the engine's process lifecycle.",
    ),
    "subagent_model": Control(
        "host",
        "Model for the LiteTUI subagent tool. Codex native delegation, including Ultra, uses its own engine configuration.",
    ),
    "tools_enabled": Control(
        "host", "Controls LiteTUI tools and the native Codex pre-tool policy gate."
    ),
    "tools_disabled": Control(
        "host",
        "Refuses matching host and native tool calls. Native registrations already in a thread may remain discoverable; inventory updates are a separate lifecycle operation.",
    ),
    "tool_policy_profile": Control(
        "host",
        "Shared host authority applies before host and native tool execution; Codex sandbox and native approvals also apply.",
    ),
    "tool_always_allow": Control(
        "host",
        "Standing host approval rules. These do not bypass Codex sandbox or native approval requirements.",
    ),
    "tool_deny": Control(
        "host", "Host refusal rules, checked for host and native tool actions."
    ),
    "enter_interrupts": Control(
        "host",
        "Controls Enter versus interrupt-and-send behavior in the host input UI.",
    ),
}


CLINE_THINKING = Control(
    "native",
    "Sent to ClinePass as the reasoning effort. Each model accepts its own levels; the list follows the selected model.",
)
CLINE_FIXED = Control(
    "unsupported",
    "ClinePass is Cline's fixed remote endpoint, so this local-server or other-engine setting does not apply. Saved values are preserved for other backends.",
)


def native(backend):
    """True only when the official app-server owns the loop (0.23.1: opt-in)."""
    return hasattr(backend, "app_server")


def control(backend, name):
    if getattr(backend, "owns_native_turns", False):
        if name in {"tools_enabled", "tools_disabled"}:
            return Control("host", "Permission changes apply immediately to Claude calls. Advertised tool inventory is fixed for a live session; use /claude new to refresh it.")
        if name in {"tool_policy_profile", "tool_always_allow", "tool_deny", "enter_interrupts"}:
            return Control("host", "LiteTUI authority and interaction setting; enforced for Claude calls.")
        if name == "thinking_level":
            return Control("native", "Claude effort level, from the CLI's model metadata. Applies from the next message; "
                           "a cache warning asks first, because the cache is built at one effort level.")
        if name in {"autocompact_enabled", "autocompact_at_percent", "clear_screen_after_compact", "wake_after_compact"}:
            return Control("host", "LiteTUI compacts Claude conversations itself (Claude's own autocompact is off): "
                           "Claude writes the summary, and the next message starts a fresh session carrying it.")
        if name in CONTROLS or name in {"codex_native_engine", "mcp_enabled", "skills_enabled"} or name.startswith("ninfer_"):
            return Control("unsupported", "Claude owns its runtime and context. This local/Codex control is unsupported; saved values are preserved for other backends.")
        return None
    if getattr(backend, "name", None) in ("cline", "free"):
        # Our loop drives ClinePass (sampling, tools, compaction all apply);
        # only its endpoint is fixed and remote.
        if name == "thinking_level":
            return CLINE_THINKING
        if CONTROLS.get(name) is LOCAL_SERVER or name in {"codex_native_engine", "claude_executable"} or name.startswith("ninfer_"):
            return CLINE_FIXED
        return None
    return CONTROLS.get(name) if native(backend) else None


def loop_description(backend, iterations):
    if getattr(backend, "owns_native_turns", False):
        return "agent loop: managed by the official Claude SDK runtime"
    if native(backend):
        return "agent loop: managed by the official Codex engine"
    return f"agent loop: up to {iterations} tool iterations per turn (/settings)"
