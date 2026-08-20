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
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

SETTINGS_FILENAME = "settings.json"

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh"]


@dataclass
class Settings:
    """Every user-facing knob. Field names are the JSON keys."""

    # ── Connection ───────────────────────────────────────────────────────────
    lm_host: str = "http://localhost:1234"

    # ── Model ────────────────────────────────────────────────────────────────
    #: Selected automatically on connect when present in the served list.
    #: None = use whatever LM Studio reports as loaded.
    default_model: str | None = None
    #: Ask LM Studio to load the model with this context length (tokens).
    #: None = leave the server's own configured length alone.
    default_context_length: int | None = None
    #: Re-apply default_model on every connect, not just the first.
    pin_default_model: bool = False

    # ── Generation ───────────────────────────────────────────────────────────
    #: Response budget WITH tools enabled. The agent loop needs headroom for
    #: tool calls plus prose; 16384 was the hardcoded value.
    max_tokens_tools: int = 16384
    #: Response budget with tools off.
    max_tokens_chat: int = 4096
    thinking_level: ThinkingLevel = "medium"

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
    tools_enabled: bool = True

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

    #: Clear the RENDERED transcript after a compaction.
    #:
    #: Not cosmetic. After compacting, the chat log still shows every message
    #: that was just REPLACED by the summary — so the screen displays history
    #: the model can no longer see, and the two silently disagree. Anyone
    #: scrolling up is reading context that is gone. Clearing makes the visible
    #: transcript mean what it appears to mean.
    clear_screen_after_compact: bool = True

    # ── Capabilities ─────────────────────────────────────────────────────────
    skills_enabled: bool = True
    mcp_enabled: bool = True
    #: Server names from mcp.json to NOT start. Absent = start everything.
    mcp_disabled_servers: list[str] = field(default_factory=list)

    # ── Interface ────────────────────────────────────────────────────────────
    show_thinking: bool = True
    autoscroll: bool = True

    # ── Footer ───────────────────────────────────────────────────────────────
    #: Each field of the status footer, individually. Defaults match what the
    #: footer showed before it was configurable, EXCEPT the percent — which was
    #: already being computed to choose the colour and then discarded.
    footer_show_seat: bool = True
    footer_show_thinking: bool = True
    footer_show_convo: bool = True
    footer_show_context: bool = True
    footer_show_context_pct: bool = True
    footer_show_tps: bool = True


#: field name → environment variable that overrides it.
#: Both pre-existing knobs are preserved by name so nothing that worked breaks.
ENV_OVERRIDES: dict[str, str] = {
    "lm_host": "LITETUI_LM_HOST",
    "tool_iterations": "LM_TOOL_ITERS",
    "default_model": "LITETUI_MODEL",
    "max_tokens_tools": "LITETUI_MAX_TOKENS",
    "thinking_level": "LITETUI_THINKING",
}


def settings_path(root: Path | None = None) -> Path:
    return (root or Path(__file__).resolve().parent) / SETTINGS_FILENAME


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
    for name, env_key in ENV_OVERRIDES.items():
        raw = os.environ.get(env_key)
        if raw is not None and raw != "":
            setattr(s, name, _coerce(name, raw, getattr(s, name)))
    return s


def save(s: Settings, root: Path | None = None) -> Path:
    """Write settings.json.

    Env-sourced fields are written too: the file records what the user CHOSE.
    Suppressing them would mean unsetting an env var silently reverts the knob
    to a default the user never picked.
    """
    p = settings_path(root)
    p.write_text(json.dumps(asdict(s), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
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
