"""Bounded LIVE check of the one-line card summary, against a resident model.

Everything else about this feature is covered by tests with a fake transport.
That proves the wiring and proves nothing about a real model: whether a real
reply comes back at all, whether it is actually ONE line, and - the part a
fake can never show - whether reasoning is really off, or merely unrequested
while the model thinks anyway.

🔴 LOADS NOTHING. It reads `lms ps`, refuses unless a model is already
resident, and names that exact identifier. Naming an unloaded model is itself
a load, so the identifier is taken from the listing and never guessed.

    python scripts/card_summary_live_check.py

Exits non-zero on any failed check.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os                                     # noqa: E402
os.environ.setdefault("LITETUI_NO_HARNESS", "1")

from openai import AsyncOpenAI                # noqa: E402

from litetui import app as app_mod            # noqa: E402
from litetui.turn_engine import _resolve_reasoning_effort   # noqa: E402
from litetui.widgets import AssistantMessage  # noqa: E402

NL = chr(10)

ANSWER = (
    "I rewrote captions.py so it fetches the timedtext URL directly with "
    "urllib instead of retrying the metadata call, because the retry path is "
    "bot-gated and a plain GET is not. The old --write path stays as a "
    "fallback, and the retry loop is gone entirely."
)


def resident_models() -> list[str]:
    """Identifiers currently loaded, straight from `lms ps`."""
    try:
        out = subprocess.run(["lms", "ps"], capture_output=True, text=True,
                             timeout=60).stdout
    except Exception as exc:
        print("BLOCKER: could not run `lms ps`:", exc)
        return []
    # `lms ps` emits a LEADING BLANK LINE, so slicing [1:] leaves the header
    # row in place. That bug made this script name a model called "IDENTIFIER";
    # LM Studio answered from the resident model instead of refusing, so the run
    # looked like a pass. Naming an unloaded model is itself a load request, so
    # the header and blanks are dropped by CONTENT, never by position.
    ids = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0].upper() in ("IDENTIFIER", "MODEL") or parts[0].startswith("-"):
            continue
        ids.append(parts[0])
    return ids


async def main() -> int:
    models = resident_models()
    if not models:
        print("BLOCKER: no model is resident. This script will NOT load one.")
        print("         Load a model in LM Studio first, then re-run.")
        return 2
    model = models[0]
    print(f"Resident models per `lms ps`: {models}")
    print(f"Using the ALREADY-RESIDENT model, loading nothing: {model}")
    if model.upper() == "IDENTIFIER" or "/" not in model and "-" not in model:
        print("BLOCKER: that does not look like a model identifier - refusing to "
              "send it, because naming an unloaded model would ask for a LOAD.")
        return 2

    app = app_mod.LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None

    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        if not cond:
            print("FAIL:", msg)
            ok = False
        else:
            print("pass:", msg)

    async with app.run_test(size=(100, 24)) as pilot:
        base = app.settings.lm_host.rstrip("/") + "/v1"
        app.client = AsyncOpenAI(base_url=base, api_key="litetui")
        print("backend:", app.backend.name, "| base_url:", base)

        effort = _resolve_reasoning_effort("off", app.backend.name)
        print("reasoning_effort actually sent:", repr(effort))
        check(effort is not None,
              "reasoning is EXPLICITLY disabled on the wire, not merely omitted")

        # Capture the real request as the transport sends it.
        seen: dict = {}
        transport = app_mod.model_transport.for_app(app)
        real_create = transport.create

        async def spy(**kw):
            seen.update(kw)
            return await real_create(**kw)
        transport.create = spy
        app_mod.model_transport.for_app = lambda _a: transport

        card = AssistantMessage()
        card.set_model_name(model)
        app.query_one("#chat-log").mount(card)
        await pilot.pause()
        card.set_answer(ANSWER)
        card.settled = True
        await pilot.pause()

        other = AssistantMessage()
        other.set_model_name("some-other-model")
        app.query_one("#chat-log").mount(other)
        await pilot.pause()

        app.model_id = "some-other-model"      # the user switches meanwhile
        try:
            await asyncio.wait_for(
                app._summarise_card(card, model, ANSWER), timeout=180)
        except asyncio.TimeoutError:
            print("BLOCKER: the live summary call did not return within 180s")
            return 2

        print(NL + "--- request as sent ---")
        print("model      :", seen.get("model"))
        print("extra_body :", json.dumps(seen.get("extra_body"), sort_keys=True))
        print("messages   :", len(seen.get("messages") or []), "message(s), roles:",
              [m.get("role") for m in (seen.get("messages") or [])])
        print(NL + "--- result ---")
        print("summary    :", repr(card.summary))
        print("header     :", card.border_title)

        check(seen.get("model") == model,
              "the request named the ORIGINATING model, not the switched-to one")
        check((seen.get("extra_body") or {}).get("reasoning_effort") == effort,
              f"reasoning_effort={effort!r} reached the wire")
        check(len(seen.get("messages") or []) == 1,
              "the side call carried ONE message and no conversation history")
        check(bool(card.summary), "a real model produced a summary")
        if card.summary:
            check(NL not in card.summary, "the summary is a single line")
            check("<think" not in card.summary.lower(),
                  "no reasoning block leaked into the summary")
            check(len(card.summary) <= AssistantMessage.MAX_SUMMARY_CHARS,
                  "the summary fits the header")
        check(str(card.border_title).endswith(f"- {model}"),
              "the header ends with the originating model name")
        check(other.summary is None,
              "the other card was NOT touched by this summary")

        # No conversation pollution: the side call must not have appended.
        check(not any("captions.py" in str(m.get("content", ""))
                      for m in app.conversation),
              "the side call did not enter the main conversation")

        # --- reopen persistence, with the REAL summary just produced --------
        import tempfile
        from litetui.conversation import ConversationRepository
        tmp = Path(tempfile.mkdtemp()) / "convo.jsonl"
        app.store.convo_path = tmp
        app.store.loading = False
        app.store.pending = False
        app._persist_card_summary(card, card.summary)

        stored = ConversationRepository.card_summaries(tmp)
        key = app._card_summary_key(ANSWER)
        check(stored.get(key) == card.summary,
              "the real summary round-trips through the store")

        _meta, restored_msgs = ConversationRepository.read(tmp)
        check(all("summary" not in m for m in restored_msgs),
              "the summary record never becomes a conversation message")

        # A reopened card carries the stored summary and must NOT re-ask.
        reopened = AssistantMessage()
        reopened.set_model_name(model)
        app.query_one("#chat-log").mount(reopened)
        await pilot.pause()
        reopened.set_answer(ANSWER)
        reopened.settled = True
        reopened.set_summary(stored.get(key))
        scheduled: list = []
        app.run_worker = lambda coro, **kw: scheduled.append(coro)
        app._kick_card_summary(reopened)
        for coro in scheduled:
            coro.close()
        check(scheduled == [],
              "a reopened card with a stored summary makes NO second model call")

    print(NL + "LIVE CHECK:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
