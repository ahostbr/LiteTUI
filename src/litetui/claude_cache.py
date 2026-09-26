"""Claude prompt-cache health, countdown and cold-restart warning (T911).

Ryan, 2026-09-24 (liteask a-3d843d20): "any time a cache would "restart" cold it
there should be a warning for the user displayed first WARNING them of the cache
hit and this will HURT there usage" ... "display a countdown in the footer for
time left on cache."

What makes the next Claude turn re-read the conversation uncached, and what this
module does about each (the cases are enumerated in the T911 report too):

  WARNED — LiteTUI can see it coming, before anything is sent:
    * model switch on a live session — the cache is per model;
    * effort change on a live session (/think, /modelcfg, /settings) — Ryan
      2026-09-24: "anytime you change effort levels, it affects the cache";
    * idle past the cache lifetime on a live session;
    * resuming a saved session whose cache has expired, or whose last use is
      unknown (a segment written before this change).
  NOT WARNED, and why:
    * the system prompt and tool set are fixed for the life of a session
      (claude_backend._options, claude_tools.sdk_options); changing them needs
      /claude new, which starts a new conversation with no history to re-read;
    * compaction is Claude's own (the host tells it not to use host compaction),
      so the CLI decides it mid-turn, where there is nothing left to warn before.

The lifetime is measured from the START of the request that read or wrote the
cache (platform.claude.com prompt-caching, "Cache lifetime"). The clock stamps
the moment a usage frame arrives, which is later than the start, so the
countdown can run slightly long; COLD_MARGIN_S treats the last two minutes as
already cold for the warning, and the footer turns amber there.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Static

from litetui.claude_backend import CACHE_TTL_SECONDS
from litetui.side_panel import close_dialog

#: Inside this many seconds of expiry the cache is treated as cold for warnings.
COLD_MARGIN_S = 120
#: What the warning dialog returns when the user chooses to send anyway.
SEND = "send"
#: What an effort change costs on Claude, said wherever one is made.
EFFORT_NOTE = ("\nApplies from your next message, on the same Claude session. The cache is "
               "built at one effort level, so a cache warning asks before that message is sent; "
               "Cancel keeps the current effort. Back to default restarts the session "
               "(resumed, conversation kept).")



@dataclass
class CacheClock:
    """The cache state of ONE Claude session segment, from its usage frames."""

    segment_id: str | None = None
    model: str | None = None
    #: The effort level the cache was built at ("default" = none sent); None
    #: until a turn has been sent on this clock.
    effort: str | None = None
    #: Wall-clock seconds of the last request that read or wrote the cache.
    used_at: float | None = None
    read: int = 0
    written: int = 0
    uncached: int = 0

    def observe(self, usage, model=None, now=None):
        """Record one per-request usage (`source == "message"`)."""
        if usage is None or getattr(usage, "source", None) != "message":
            return False
        read = usage.cache_read_tokens or 0
        written = usage.cache_creation_tokens or 0
        self.read, self.written, self.uncached = read, written, usage.input_tokens or 0
        if model:
            self.model = model
        if read or written:
            self.used_at = time.time() if now is None else now
        return True

    def remaining(self, now=None):
        if self.used_at is None:
            return None
        return CACHE_TTL_SECONDS - ((time.time() if now is None else now) - self.used_at)

    def hit_ratio(self):
        total = self.read + self.written + self.uncached
        return self.read / total if total else None

    def label(self, now=None):
        """(text, style) for the footer, or None before the first request."""
        left = self.remaining(now)
        if left is None:
            return None
        if left <= 0:
            return "cache cold", "bold #e5534b"
        ratio = self.hit_ratio()
        hit = f" {ratio * 100:.0f}%" if ratio is not None else ""
        mins, secs = divmod(int(left), 60)
        clock = f"{mins}:{secs:02d}" if left <= COLD_MARGIN_S else f"{mins}m"
        style = "#e8a33d" if left <= COLD_MARGIN_S else ("#98c379" if self.read else "#7d8799")
        return f"cache {'warm' if self.read else 'new'}{hit} {clock}", style


def codex_label(last_usage):
    """(text, style) for a Codex seat's footer cache field (T992), or None.

    Read from app.last_usage, which both Codex paths already fill. App-server:
    latest_request_usage is a dict holding the last request's inputTokens /
    cachedInputTokens; its top-level prompt_tokens / cached_tokens are the TURN
    AGGREGATE, so they are never used here. A snapshot without a per-request
    inputTokens is unmeasured. Responses path: latest_request_usage is None
    (_record_usage stores every key) and the top-level prompt_tokens /
    cached_tokens describe the one request. Codex gives no cache lifetime, so
    there is no countdown and no "expires" claim: warm/cold says only whether
    the last request read from the cache. Unmeasured = absent."""
    usage = last_usage or {}
    last = usage.get("latest_request_usage")
    if isinstance(last, dict):
        inp, cached = last.get("inputTokens"), last.get("cachedInputTokens")
    else:
        inp, cached = usage.get("prompt_tokens"), usage.get("cached_tokens")
    if not inp or cached is None:
        return None
    if not cached:
        return "cache cold 0%", "#7d8799"
    return f"cache warm {cached / inp * 100:.0f}%", "#98c379"


def _ago(seconds):
    mins = int(seconds // 60)
    return f"{mins} min" if mins < 120 else f"{mins // 60} h"


def cold_reason(clock, *, live, resuming, model, used_at=None, now=None, effort=None):
    """(kind, sentence) for why the next turn would re-read the conversation
    uncached, or None.

    `live`: a session is already running for this segment. `resuming`: no live
    session, but the segment names a saved native session. `used_at`: the
    persisted last cache use for a resume (None = unknown). `effort`: the level
    the next turn runs at (None = the model default)."""
    now = time.time() if now is None else now
    if live:
        if clock.model and model and clock.model != model:
            return "model", (f"Switching model from {clock.model} to {model}. Each model keeps its own cache, "
                    "so Claude re-reads this whole conversation at full input price.")
        effort = effort or "default"
        if clock.effort and clock.effort != effort:
            # Ryan 2026-09-24: "anytime you change effort levels, it affects the cache."
            return "effort", (f"Changing effort from {clock.effort} to {effort}. The cache is built at one effort "
                    "level, so Claude re-reads this whole conversation at full input price.")
        left = clock.remaining(now)
        if left is not None and left <= COLD_MARGIN_S:
            return "expired", (f"The prompt cache has expired (last used {_ago(now - clock.used_at)} ago; it lasts "
                    f"{CACHE_TTL_SECONDS // 60} min). Claude re-reads this whole conversation at full input price.")
        return None
    if resuming:
        if used_at is None:
            return "resume", ("Resuming a saved Claude session whose cache age is unknown. If its cache has expired, "
                    "Claude re-reads the whole conversation at full input price.")
        if now - used_at >= CACHE_TTL_SECONDS - COLD_MARGIN_S:
            return "resume", (f"Resuming a saved Claude session last used {_ago(now - used_at)} ago; its cache "
                    "has expired. Claude re-reads the whole conversation at full input price.")
    return None


WARNING_TAIL = ("\n\nThis counts against your plan usage. On a long conversation it can "
                "cost as much as many normal turns.")


class CacheWarningBody(Widget):
    """Send-anyway / cancel, in either dialog host (see vram_dialog)."""

    DEFAULT_CSS = """
    CacheWarningBody { height: auto; layout: vertical; }
    CacheWarningBody #cache-title { text-style: bold; color: #e5534b; padding: 0 0 1 0; }
    CacheWarningBody #cache-text { padding: 0 0 1 0; }
    CacheWarningBody #cache-buttons { height: auto; align: center middle; }
    CacheWarningBody #cache-buttons Button { margin: 0 1 0 0; }
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static("Warning: this turn will hurt your usage", id="cache-title")
        yield Static(self._text + WARNING_TAIL, markup=False, id="cache-text")
        with Horizontal(id="cache-buttons"):
            yield Button("Send anyway", variant="warning", id="cache-send")
            yield Button("Cancel", variant="primary", id="cache-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        close_dialog(self, SEND if event.button.id == "cache-send" else None)

    def get_state(self) -> dict:
        return {}

    def set_state(self, state: dict) -> None:
        return None


class CacheWarningScreen(ModalScreen):
    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield CacheWarningBody(self._text)


#: A headless host confirms by sending again within this window.
RPC_ACK_S = 120


async def confirm_cold(app, cold):
    """Show the warning FIRST. True = send anyway.

    A headless (rpc) child has no keyboard: the first attempt announces the
    warning to its host and is not sent; sending again within RPC_ACK_S for the
    same kind of cold start is the go-ahead."""
    from functools import partial

    from litetui.side_panel import show_dialog

    kind, reason = cold
    if getattr(app, "_rpc", None):
        ack = getattr(app, "_claude_cache_ack", None)
        if ack and ack[0] == kind and time.time() - ack[1] < RPC_ACK_S:
            app._claude_cache_ack = None
            return True
        app._claude_cache_ack = (kind, time.time())
        app._system("Cache warning: " + reason + " Send again within 2 minutes to go ahead.")
        app._rpc_emit({"type": "cache_warning", "provider": "claude", "kind": kind, "reason": reason})
        return False
    answer = await show_dialog(app, partial(CacheWarningBody, reason),
                               modal_factory=partial(CacheWarningScreen, reason))
    return answer == SEND


def clock_for(app, segment_id):
    """The app's clock for this segment; a different segment starts a fresh one."""
    clock = getattr(app, "_claude_cache", None)
    if clock is None or clock.segment_id != segment_id:
        clock = CacheClock(segment_id=segment_id)
        app._claude_cache = clock
    return clock


def ensure_ticker(app):
    """Refresh the footer so the countdown moves between turns."""
    if getattr(app, "_claude_cache_ticker", None) is None and hasattr(app, "set_interval"):
        app._claude_cache_ticker = app.set_interval(15, app._refresh_ctx_label)
