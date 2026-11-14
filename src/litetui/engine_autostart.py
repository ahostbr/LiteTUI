"""A send with nothing serving starts the chosen engine, then sends (T1311).

Ryan, 2026-11-14: he picked Edge0 in /model, sent "hello", and was told *"Nothing is serving
on ninfer yet ... /engine start serves the chosen model"*. His pick, word for word: **"Send
auto-starts"** — sending when nothing is serving starts the chosen model, and sends once it
has loaded, instead of the "Nothing is serving" line. He did NOT pick "Pick = serve": /model
still only chooses, so nothing here runs on a /model pick.

🔴 A PERSON AT THE KEYBOARD ONLY. Starting an engine puts tens of GB in VRAM, and the
standing rule is that nothing loads a model without a person asking. A headless `--rpc`
child, a spawned seat and a message that did not come from the keyboard (an inbox wake, a
GUI rpc, a compaction resend) never start one: they keep today's refusal words.

The held message rides the queue that already exists for "hold it, send it later"
(`_pending_input`, the same one MCP maintenance uses), and `_flush_pending_input` sends it.
"""
from __future__ import annotations

from . import llm_backend


def nothing_serving(app) -> bool:
    """The state that produced Ryan's line: an engine backend with no served model."""
    backend = getattr(app, "backend", None)
    if getattr(backend, "name", "") not in llm_backend.ENGINE_BACKENDS:
        return False
    if not callable(getattr(backend, "start_engine", None)):
        return False
    session = getattr(app, "_agent_session", None)
    if session is not None and getattr(session.authority, "model", None) is None:
        return True
    return not getattr(app, "model_id", "")


def at_keyboard(app, source: str) -> bool:
    """Typed by a person into this window: not `--rpc`, not a spawned seat, not a resend."""
    return (source == "typed" and not getattr(app, "_rpc", False)
            and not getattr(app, "_spawned_marker", False))


def should_hold(app, source: str) -> bool:
    """Hold this message for an engine start: one is already under way for an earlier send,
    or this send is the one that starts it."""
    if getattr(app, "_engine_autostart", False):
        return True
    return nothing_serving(app) and at_keyboard(app, source)


def hold(app, item: dict) -> None:
    """Queue `item` (a `_pending_input` row) and start the engine if no start is running."""
    app._pending_input.append(item)
    held = getattr(app, "_engine_autostart_held", None)
    if held is None:
        held = app._engine_autostart_held = []
    held.append(item)
    if getattr(app, "_engine_autostart", False):
        app.notify("Queued — sends once the engine is ready", timeout=3)
        return
    app._engine_autostart = True
    backend = app.backend
    label = getattr(backend, "label", backend.name)
    app.system_message(f"Nothing is serving on {backend.name} — starting {chosen(app)}. Your message "
                       f"sends once {label} has loaded it; /engine stop cancels.")
    app.run_worker(run(app, backend), exclusive=False, name="engine-autostart", exit_on_error=False)


def chosen(app) -> str:
    """The model the start will serve, by the name /model showed (from the settings the start reads)."""
    s = getattr(getattr(app, "backend", None), "_settings", None) or getattr(app, "settings", None)
    name = getattr(getattr(app, "backend", None), "name", "")
    if name == "ninfer":
        from pathlib import Path
        art = str(getattr(s, "ninfer_artifact", "") or "").strip()
        return Path(art).stem if art else "the chosen model"
    engine = getattr(app.backend, "_engine", None)
    try:
        path = engine.chosen_config(s)
    except Exception:  # noqa: BLE001 - a name for a sentence is never worth a failure
        path = None
    return path.stem.removeprefix("strata-") if path else "the chosen model"


async def run(app, backend) -> None:
    """Start, wait for the reconnect, then send what was held — or give it back with why."""
    from .plugins.model_switch import start_and_connect

    ok = False
    try:
        started = await start_and_connect(app, backend)
        if started:
            wait = getattr(started, "wait", None)
            if wait is not None:
                try:
                    await wait()
                except Exception:  # noqa: BLE001 - a failed/cancelled reconnect said its own words
                    pass
            ok = app.backend is backend and not nothing_serving(app)
            if not ok:
                app.system_message(f"{getattr(backend, 'label', backend.name)} started, but no model "
                                   "came up on the reconnect.")
    finally:
        app._engine_autostart = False
        held = getattr(app, "_engine_autostart_held", None) or []
        app._engine_autostart_held = []
        if ok:
            app._flush_pending_input()
        else:
            give_back(app, held)


def give_back(app, held: list[dict]) -> None:
    """A start that did not serve: take the held messages off the queue (so nothing sends them
    to an engine that is not there) and put their text back in the input box."""
    texts = []
    for item in held:
        try:
            app._pending_input.remove(item)
        except ValueError:
            continue                        # already sent or dropped elsewhere
        bubble = item.get("bubble")
        if bubble is not None:
            try:
                bubble.remove()
            except Exception:  # noqa: BLE001 - a bubble already gone is not news
                pass
        if item.get("text"):
            texts.append(item["text"])
        content = item.get("content")
        if isinstance(content, list) and not getattr(app, "pending_image", None):
            for part in content:                # an attached image comes back attached
                url = (part.get("image_url") or {}).get("url", "") if isinstance(part, dict) else ""
                if url.startswith("data:") and "," in url:
                    app.pending_image = url.split(",", 1)[1]
                    break
    if not texts:
        if getattr(app, "pending_image", None):
            app.system_message("Your message was not sent, because the engine is not serving — "
                               "its image is attached again.")
        return
    restored = "\n\n".join(texts)
    try:
        from .widgets import PromptInput

        box = app.query_one("#message-input", PromptInput)
        box.value = f"{restored}\n\n{box.value}".strip() if box.value else restored
        where = "it is back in the input box"
    except Exception:  # noqa: BLE001 - no input box (a test double): say the words instead
        where = f"it was: {restored[:200]}"
    app.system_message(f"Your message was not sent, because the engine is not serving — {where}.")
