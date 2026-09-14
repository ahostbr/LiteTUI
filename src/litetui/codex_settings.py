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
CONTROLS = {
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


def control(backend, name):
    return CONTROLS.get(name) if backend == "codex" else None


def loop_description(backend, iterations):
    if backend == "codex":
        return "agent loop: managed by the official Codex engine"
    return f"agent loop: up to {iterations} tool iterations per turn (/settings)"
