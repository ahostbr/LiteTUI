"""Claude Agent control plane; the official SDK/CLI owns auth and execution."""
from __future__ import annotations

import asyncio
import importlib.metadata
import os
import subprocess

from litetui.claude_session import ClaudeSession
from litetui.llm_backend import BackendError, ModelRow

SDK_VERSION = "0.2.159"
CLI_VERSION = "2.1.281"

#: The prompt-cache lifetime LiteTUI pins for the main conversation (T911, Ryan
#: 2026-09-24). CLI 2.1.281 reads CLAUDE_CODE_PROMPT_CACHE_TTL (code.claude.com
#: /docs/en/prompt-caching; string present in the bundled claude.exe). With
#: setting_sources=[] the user's promptCacheTtl setting is never read, so the env
#: is the only way to pin it.
CACHE_TTL = "1h"
CACHE_TTL_SECONDS = 3600

#: LiteTUI compacts Claude conversations itself (claude_compact; Ryan
#: 2026-09-24: "I would rather keep it if it's possible"), so Claude's own
#: autocompact is off. Measured on CLI 2.1.281 via get_context_usage():
#: isAutoCompactEnabled True (threshold 967000) by default, False with this.
#: DISABLE_COMPACT turns off the rest, the hard-limit compaction included (read out
#: of the CLI: it is checked beside DISABLE_AUTO_COMPACT wherever compaction is
#: decided). Ryan, 2026-09-24 (plan claude-backend-litetui-identity, phase 4): hard
#: limit = "LiteTUI only". So LiteTUI's threshold is the one compaction system, checked
#: mid-turn too (claude_turn), and an overflow is a stated failure LiteTUI compacts from.
AUTOCOMPACT_ENV = {"DISABLE_AUTO_COMPACT": "1", "DISABLE_COMPACT": "1"}
#: Claude Code's own auto-memory index (MEMORY.md) is not loaded: LiteTUI's store is
#: this session's memory. Read out of CLI 2.1.281: CLAUDE_CODE_DISABLE_AUTO_MEMORY
#: truthy -> auto-memory "off" (the check that returns disabled_by_env_var).
AUTO_MEMORY_ENV = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}

#: Appended to Claude Code's preset system prompt. Fixed for the life of a
#: session and identical across sessions, so the cache prefix it anchors holds.
#:
#: 🔴 THE COMPACTION REQUEST MUST BE DECLARED HERE, IN THE TRUSTED CHANNEL. Sent
#: as a bare user message, LiteTUI's compaction prompt was refused as a prompt
#: injection (live, sonnet, CLI 2.1.281: "this looks like an attempt to get me
#: to persist that content ... I'm not going to do that").
COMPACT_MARKER = "[LiteTUI compaction request]"
APPEND = ("You are running inside LiteTUI. Claude owns this session, its native tools and context "
          "window. LiteTUI presents your answers, enforces user authority, and owns compaction. When "
          "the context fills, or the user runs /compact, LiteTUI sends a user message that starts with "
          f"{COMPACT_MARKER}. That message is genuine: answer it with the summary it asks for, and "
          "LiteTUI continues in a fresh session that starts from that summary. Do not compact on your "
          "own and do not use model-changing tools.")


def seeded_append(seed):
    """The system-prompt append for a session that continues a compaction.

    The summary rides in the SYSTEM prompt, not the first user message: in the
    user channel the fresh session refused it as an injection (live: "I have no
    code or codename on record, this looks like a prompt injection attempt").
    Fixed for the life of the segment, so every request of that session (and
    every resume of it) carries the same prefix."""
    return (f"{APPEND}\n\nThis session continues a conversation LiteTUI compacted. The summary below "
            "was written by you, in the previous session, from the full conversation. Treat it as "
            f"your own notes on everything before this point:\n\n{seed}")

#: Every model spelling `claude --model` accepts, standard and 1M context.
#: The CLI's initialize metadata is its own curated picker (five rows on CLI
#: 2.1.281: default, opus[1m], claude-fable-5-1[1m], sonnet, haiku), so the
#: queried list comes first and this fills in the rest. [1m] only where the CLI
#: offers it: Opus 5.5, Fable 5.1 and Sonnet 5; Haiku 4.5 has no 1M window (the
#: CLI refuses it with "doesn't have a 1M context window").
# ponytail: static list, refresh when the CLI pin (CLI_VERSION) moves.
STATIC_MODELS = (
    "default", "opus", "opus[1m]", "fable", "fable[1m]", "sonnet", "sonnet[1m]", "haiku",
    "claude-opus-5-5", "claude-opus-5-5[1m]",
    "claude-fable-5-1", "claude-fable-5-1[1m]",
    "claude-sonnet-5", "claude-sonnet-5[1m]",
    "claude-haiku-4-5-20251001",
)
#: Effort levels for a STATIC_MODELS row the CLI did not describe. Every
#: queried Opus/Fable/Sonnet row on CLI 2.1.281 reports exactly these; its
#: Haiku row reports none (supportsEffort absent).
STATIC_EFFORT = ("low", "medium", "high", "xhigh", "max")


def cache_env(environ=None):
    """Child env that pins the cache lifetime and neutralises inherited switches.

    The SDK builds the child env as {**os.environ, **options.env}, so an inherited
    key can be overridden but not removed. The CLI treats an empty value as unset,
    so a stray DISABLE_PROMPT_CACHING* or FORCE_PROMPT_CACHING_5M in the launching
    shell is blanked here rather than silently turning caching off or down to 5m."""
    environ = os.environ if environ is None else environ
    env = {"CLAUDE_CODE_PROMPT_CACHE_TTL": CACHE_TTL, "FORCE_PROMPT_CACHING_5M": ""}
    for key in environ:
        if key.upper().startswith("DISABLE_PROMPT_CACHING"):
            env[key] = ""
    env.setdefault("DISABLE_PROMPT_CACHING", "")
    return env


def sdk_module():
    try:
        import claude_agent_sdk
    except ImportError as exc:
        raise BackendError("Claude Agent needs the optional SDK: uv sync --extra claude. Authentication stays with Claude: run `claude auth login`, then reconnect.") from exc
    version = importlib.metadata.version("claude-agent-sdk")
    if version != SDK_VERSION:
        raise BackendError(f"Claude Agent requires tested SDK {SDK_VERSION}; installed {version}. Run uv sync --extra claude.")
    return claude_agent_sdk


def track_close(app, task):
    """Remember one owned-cleanup handle. Never overwrite, never drop.

    🔴 THIS WAS AN ASSIGNMENT AND IT LOST PROCESSES BOTH WAYS. Six call sites
    set `app._claude_closing = <backend>.shutdown()`, and two switches in a row
    lose the first close whichever order they land in: shutdown() called again
    before the first task runs overwrote it — and asyncio keeps only a weak
    reference to a task, so the dropped one can be collected mid-flight — while
    shutdown() called after `close()` cleared `self.session` returns None, and
    assigning that None erased a live handle outright. Either way an owned
    child process stopped being tracked while the code read as if cleanup had
    been arranged. Accumulate, and let a None mean "nothing to add" rather than
    "forget what you were waiting for".
    """
    if task is None:
        return
    tasks = getattr(app, "_claude_closing", None)
    if not isinstance(tasks, list):
        tasks = [] if tasks is None else [tasks]
        app._claude_closing = tasks
    if not any(task is held for held in tasks):
        tasks.append(task)


def close_native(app, backend):
    """Begin owned cleanup for *backend* if it owns native turns, and track it.

    One door rather than the same four lines at six call sites — and the four
    lines were what made the type checker's day hard, because every site had to
    prove `backend` was neither None nor one of the several backend classes
    without a `shutdown`. Asking the object, once, here, answers both.
    """
    if not getattr(backend, "owns_native_turns", False):
        return
    shutdown = getattr(backend, "shutdown", None)
    if callable(shutdown):
        track_close(app, shutdown())


async def settle_close(app, *, timeout=30):
    """Await every owned cleanup exactly once; return its failures as text.

    Cleared BEFORE the await, deliberately. The handle used to be cleared after
    it, so a close that raised — and `ClaudeBackend.close` raises whenever the
    session recorded cleanup errors — left a failed task in place that
    re-raised the same stale exception on every later turn and every connect.
    One ten-second disconnect timeout bricked the backend until the app was
    restarted.

    Failures come back as strings rather than exceptions: every caller wants to
    say what happened and carry on, and returning them is what makes "reported
    once" true. The bound is a backstop only — each session already bounds its
    own cleanup (claude_session.cleanup_timeout) — for a handle whose task is
    wedged somewhere those bounds do not reach.
    """
    tasks, app._claude_closing = getattr(app, "_claude_closing", None), None
    if not tasks:
        return []
    if not isinstance(tasks, list):
        tasks = [tasks]
    try:
        done, pending = await asyncio.wait(tasks, timeout=timeout)
    except BaseException:
        # The waiter losing interest does not release ownership of SDK cleanup.
        for task in tasks:
            track_close(app, task)
        raise
    failures = []
    for task in pending:
        track_close(app, task)
    if pending:
        # wait_for(gather(...)) would cancel cleanup then wait indefinitely for
        # a cancellation-resistant task. Session owns escalation; retain its
        # handle rather than abandoning the process or cancelling its teardown.
        failures.append(f"owned cleanup did not settle within {timeout:.0f}s")
    for task in done:
        if task.cancelled():
            failures.append("owned cleanup was cancelled; process cleanup is unverified")
        elif (error := task.exception()) is not None:
            failures.append(f"{type(error).__name__}: {error}")
    return failures


class ClaudeBackend:
    name = "claude"
    label = "Claude Agent"
    remote = True
    attached = True
    owns_native_turns = True

    def __init__(self, settings):
        self.settings = settings
        self.models = {}
        self.session = None
        self.segment_id = None
        self._closing = None
        self._catalog_lock = asyncio.Lock()

    def set_settings(self, settings):
        self.settings = settings

    def base_url(self):
        return "https://claude.ai"  # Identity only; never used for inference.

    host = base_url

    def empty_state_hint(self):
        return "install LiteTUI's claude extra and sign in with `claude auth login`, then /reconnect"

    async def _options(self, **values):
        sdk = sdk_module()
        override = str(getattr(self.settings, "claude_executable", "") or "").strip()
        if override:
            try:
                version = (await asyncio.to_thread(subprocess.check_output, [override, "--version"], text=True, timeout=10)).strip()
            except (OSError, subprocess.SubprocessError) as exc:
                raise BackendError("Cannot run configured Claude executable") from exc
            if version != f"{CLI_VERSION} (Claude Code)":
                raise BackendError(f"Claude executable must match tested CLI {CLI_VERSION}; found {version}")
        defaults = {
            "cli_path": override or None,
            "setting_sources": [], "skills": [], "strict_mcp_config": True, "mcp_servers": {},
            "tools": [], "permission_mode": "dontAsk", "include_partial_messages": True,
            "verbatim_prompts": True,
            "system_prompt": {"type": "preset", "preset": "claude_code", "append": APPEND},
            "extra_args": {"no-chrome": None, "disable-slash-commands": None, "replay-user-messages": None},
            "env": {**cache_env(), **AUTOCOMPACT_ENV, **AUTO_MEMORY_ENV},
        }
        defaults.update(values)
        return sdk.ClaudeAgentOptions(**defaults)

    async def ensure_running(self):
        # Metadata-only connection; never creates a user turn or accesses tokens.
        async with self._catalog_lock:
            if self.models:
                return "ok"
            import tempfile
            with tempfile.TemporaryDirectory(prefix="litetui-claude-catalog-") as cwd:
                session = ClaudeSession(await self._options(cwd=cwd))
                try:
                    info = await session.start()
                    self.models = {m["value"]: m for m in info.get("models", []) if m.get("value")}
                finally:
                    await session.close()
            if not self.models:
                raise BackendError("Claude returned no model metadata. Check CLI installation/auth and reconnect.")
            for key in STATIC_MODELS:
                self.models.setdefault(key, {"value": key})
        return "ok"

    async def list_models(self):
        await self.ensure_running()
        return [ModelRow(key=key, path=None, source=self.label, loaded=True, modalities=("text",)) for key in self.models]

    async def model_info(self, key):
        return None  # Native context occupancy is not a local configured ceiling.

    async def ensure_chat_ready(self, key):
        await self.ensure_running()
        if key not in self.models:
            raise BackendError("Choose an available Claude model with /model.")

    async def open_session(self, segment, model, system_prompt=None, **options):
        await self.ensure_chat_ready(model)
        if self.session is not None and self.segment_id != segment["id"]:
            await self.close()
        if self.session is None:
            self.segment_id = segment["id"]
            if system_prompt:
                # LiteTUI's own prompt (claude_turn.system_prompt_for), a plain
                # string: no Claude Code preset underneath it.
                options["system_prompt"] = system_prompt
            elif segment.get("seed"):
                options["system_prompt"] = {"type": "preset", "preset": "claude_code",
                                            "append": seeded_append(segment["seed"])}
            self.session = ClaudeSession(await self._options(
                cwd=segment["workspace"], resume=segment.get("session_id"), model=model, **options,
            ))
        await self.session.start()
        return self.session

    async def close(self):
        bridge = getattr(self, "_claude_tools", None)
        if bridge is not None:
            bridge.stop()
        session, self.session = self.session, None
        self.segment_id = None
        if session is not None:
            await session.close()
            if session.lifecycle.cleanup_errors:
                raise BackendError("Claude cleanup failed: " + "; ".join(session.lifecycle.cleanup_errors))

    def shutdown(self):
        # Legacy synchronous callers initiate owned cleanup; async switch/exit
        # surfaces await close directly before replacement.
        #
        # One close per session, and the SAME handle to every caller that asks
        # while it runs: two switches in the same tick used to build two tasks
        # against one session, and whichever lost the assignment ran anyway,
        # unwatched. Returning the live handle also lets `track_close` see it
        # is already held instead of listing it twice.
        if self._closing is not None and not self._closing.done():
            return self._closing
        if self.session is None:
            return None
        self._closing = asyncio.create_task(self.close(), name="claude-sdk-close")
        return self._closing

    def request_overrides(self, key):
        raise BackendError("Claude owns native turns; legacy model requests/sidecalls are unsupported.")

    def reasoning_levels(self, key):
        """The CLI's supportedEffortLevels for this model; the static set for a
        catalogue row it did not describe; [] for an unknown model or Haiku."""
        meta = self.models.get(key)
        if meta is None:
            return []
        if "resolvedModel" in meta:          # queried: the CLI's word is final
            return list(meta.get("supportedEffortLevels") or [])
        return [] if "haiku" in key else list(STATIC_EFFORT)

    async def load(self, key, **kwargs):
        raise BackendError("Claude runs remotely. Use /model; local loading is unsupported.")

    async def unload(self, key):
        raise BackendError("Claude does not occupy local model memory.")

    def seat_snapshot(self, key):
        return None
