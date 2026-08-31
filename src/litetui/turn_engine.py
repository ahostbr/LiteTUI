"""The decisions a turn makes, with no opinion about how the turn looks.

Extracted from `app.LiteTUI` (finding 4, second half). The split is not
"the big methods leave" -- it is **decisions leave, rendering stays**.

WHY `_stream` ITSELF DID NOT MOVE, MEASURED RATHER THAN ASSUMED
    `_stream` makes 58 UI attribute accesses across its 375 lines (AST count:
    widget mounts, `_scroll_down`, `call_after_refresh`, the `ctx_used`
    reactive, per-token `body.content` writes). It IS the view. Relocating it
    behind a host reference would replace 58 direct calls with 58 indirect
    ones, add a hop to the hottest loop in the app, and improve nothing --
    an extraction in name that makes the code worse to read.

    What was actually tangled up inside that loop was pure data work: how a
    request dict is assembled, and how streamed tool-call deltas are folded
    together. Those had no business being unreachable without a Textual mount.
    They are here, and they are testable with no app at all.

THE TOOL-AUTHORITY DOOR IS NOT HERE, DELIBERATELY
    `_execute_tool` stays on the host. It needs `push_screen_wait` for the
    CONFIRM dialog and the host's active tool profile, so authorization lives
    where the UI lives. Nothing in this module invokes a tool. The invariant
    -- exactly one `asyncio.to_thread(fn, ...)` in the package, inside
    `_execute_tool` -- is checked by `tools/tool_door_gate.py`, which walks the
    AST rather than grepping, because the package-wide grep matches a
    docstring in `ask_user_question.py` that merely describes the door.

THE TWO REQUEST BUILDERS ARE SEPARATE ON PURPOSE
    A chat turn and a compaction turn look similar and are not the same
    request: the compaction sends no sampling overrides, no `stream_options`,
    a different `max_tokens`, and reads its thinking level from a different
    setting. Merging them into one builder with flags would be a redesign,
    and this is a behaviour-preserving refactor. Each is its own function and
    each produces byte-identical output to the inline code it replaced.
"""

from __future__ import annotations

from litetui import llm_backend


class TurnEngine:
    """Pure turn decisions. No widgets, no Textual, no network, no disk."""

    # ── compaction policy ────────────────────────────────────────────────

    @staticmethod
    def autocompact_due(
        *,
        enabled: bool,
        at_percent: int,
        ctx_max: int | None,
        ctx_used: int | None,
        ctx_loaded: bool,
        failed_at: int | None,
    ) -> int | None:
        """The window percent when a compaction is DUE, else None.

        A TEST — it never starts one. Split out so the agent loop can ASK
        between tool iterations without acting: `_stream` and `_compact` are
        both `@work(exclusive=True, group="chat")`, so starting a compaction
        from inside the loop would cancel the loop that started it, possibly
        between an assistant message carrying tool_calls and its results.

        Every `None` below is a refusal to guess, and each refusal is load
        bearing:

        - no window size -> never guess a threshold.
        - model NOT LOADED -> `ctx_max` is the model's CEILING, not its window.
          80% of 262,144 is 209,715 tokens, which an 8k window can never reach,
          so this would silently never fire and the model would blow its real
          context instead.
        - the window has not moved since the last FAILED compaction -> the
          inputs are identical, so the next attempt fails identically, and each
          attempt is a full request. This is the retry loop watched in the
          wild: "Compacting 126 messages / Compact failed / Compacting 126
          messages", with no backoff.
        """
        if not enabled:
            return None
        if not ctx_max or not ctx_used:
            return None
        if not ctx_loaded:
            return None
        if ctx_used == failed_at:
            return None
        pct = ctx_used * 100 // ctx_max
        if pct < at_percent:
            return None
        return pct

    # ── request assembly ─────────────────────────────────────────────────

    @staticmethod
    def chat_request(
        *,
        model_id: str | None,
        messages: list[dict],
        tools_enabled: bool,
        max_tokens_tools: int,
        max_tokens_chat: int,
        request_overrides: dict,
        thinking_level: str | None,
        tools: list[dict] | None = None,
    ) -> dict:
        """The request a normal streamed turn sends."""
        kwargs: dict = {
            "model": model_id or "local-model",
            # The live store is merged into `messages` by the caller, not held
            # on the conversation itself.
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens_tools if tools_enabled else max_tokens_chat,
            "stream_options": {"include_usage": True},
        }
        # Sampling: global /settings defaults with this model's Inference
        # overrides layered on top (only values actually SET are sent — an
        # unset knob must leave the server's own default in charge).
        # Split native-vs-extra_body: the OpenAI client's create() has typed
        # params and no **kwargs, so top_k/min_p/repeat_penalty as top-level
        # keys are a TypeError, not a passthrough.
        native, extra, response_format = llm_backend.split_request_kwargs(
            request_overrides
        )
        kwargs.update(native)
        if response_format is not None:
            kwargs["response_format"] = response_format
        # 🔴 ADVERTISED EVEN WHEN TOOLS ARE OFF (T073). Withholding the schemas
        # does not stop the model calling a tool — it stops it calling one
        # STRUCTURALLY. With tools off, qwen3.8 emitted literal tool_call markup
        # as PLAIN REPLY TEXT and the turn died there, because the only tool
        # handling anywhere is the structured delta.tool_calls path and nothing
        # parses the text form. The conversation history still carries earlier
        # tool_calls and role:"tool" results from when tools were ON, so the
        # model was imitating its own transcript.
        # With the schemas present it takes the structured path, _execute_tool
        # refuses before any side effect, and the turn continues.
        # `max_tokens` above still follows tools_enabled: that is a BUDGET, not
        # an advertisement.
        if tools is not None:
            kwargs["tools"] = tools
        # reasoning_effort rides extra_body so the value lands in the JSON
        # verbatim: the client types it as a fixed Literal, and two of LM
        # Studio's six ("none", "xhigh") are not in it.
        # The per-model Thinking Level (/modelcfg Inference tab) arrives here
        # already merged into extra; blank there means inherit the global.
        level = extra.pop("reasoning_effort", None) or thinking_level
        if level:
            extra["reasoning_effort"] = "none" if level == "off" else level
        if extra:
            kwargs["extra_body"] = extra
        return kwargs

    @staticmethod
    def compact_request(
        *,
        model_id: str | None,
        messages: list[dict],
        max_tokens: int,
        thinking_level: str | None,
        request_overrides: dict | None = None,
        tools_enabled: bool,
        tools: list[dict] | None = None,
    ) -> dict:
        """The request a compaction turn sends.

        STREAMED, so the card can show the summary being born — the old call
        was stream=False and the whole act was a black box between
        "Compacting..." and the ledger.

        The thinking level must be one the model actually accepts: a virtual
        model whose level set lacks "none" DROPS the field with a 200 and
        reasons at ITS OWN default anyway. See the host's
        `_warn_reasoning_ignored`, which detects that in band.
        """
        # The per-model Thinking Level wins over the compact global; blank
        # there means inherit it — same rule as a chat turn.
        level = (request_overrides or {}).get("reasoning_effort") or thinking_level
        # Symmetric with the chat arm: an absent level must leave the server's
        # own default in charge, not send a null. 'off' is this app's word;
        # the wire says 'none'.
        extra_body: dict = {}
        if level:
            extra_body["reasoning_effort"] = "none" if level == "off" else level
        kwargs: dict = {
            "model": model_id or "local-model",
            "messages": messages,
            "stream": True,
            "max_tokens": max_tokens,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools is not None:
            # Passed so STEP 1 of COMPACT_PROMPT can actually happen. Without
            # them the instruction to persist is theatre.
            # Advertised even when tools are off, for the reason in
            # chat_request: the schemas are what keep the model on the
            # structured path. The call is then refused, not executed.
            kwargs["tools"] = tools
        return kwargs

    # ── streamed tool-call assembly ──────────────────────────────────────

    @staticmethod
    def accumulate_tool_call(tool_acc: dict, tc) -> tuple[int, bool, bool]:
        """Fold ONE streamed tool-call delta into the accumulator.

        A tool call arrives in pieces across chunks: the id in one, the name
        possibly split over several, the JSON arguments a character at a time.
        Concatenation -- not assignment -- is what makes that correct, and it
        is why `name` and `arguments` are `+=`.

        Returns (index, named_now, argued_now). The two flags are what the
        caller needs to decide whether a widget has a name to show yet; the
        caller owns that decision because this module owns no widgets.
        """
        idx = tc.index
        slot = tool_acc.setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if tc.id:
            slot["id"] = tc.id
        named_now = False
        argued_now = False
        fn = tc.function
        if fn is not None:
            if fn.name:
                slot["name"] += fn.name
                named_now = True
            if fn.arguments:
                slot["arguments"] += fn.arguments
                argued_now = True
        return idx, named_now, argued_now
