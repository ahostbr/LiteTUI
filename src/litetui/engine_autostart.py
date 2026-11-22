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


def waiting(app) -> bool:
    """A start this app is waiting on (any path: a send, /engine start, /engine restart)."""
    return bool(getattr(app, "_engine_autostart", False))


def should_hold(app, source: str) -> bool:
    """Hold this message for an engine start: one is already being waited on, or this send
    (from a person) finds nothing serving and so starts one — or waits for a start already
    in flight that nothing in this app is waiting on (Prefix review cda5cb84)."""
    if waiting(app):
        return True
    return nothing_serving(app) and at_keyboard(app, source) and declined_words(app, source) is None


# -- a start that fails the same way every time (T1338) ----------------------------------------
#
# Measured: a model with --max-context 150000 --max-concurrency 2 needed 7.2 GiB more VRAM than the
# card had, and every send started it again: five spawns in six minutes, five identical failures,
# each loading 20.8 GiB of weights to find out. A start that failed for a reason the same
# settings will hit again is remembered with those settings; a send under the SAME settings says so
# instead of spawning. A different setting, or an explicit /engine start, tries again.

_KEY_FIELDS = ("ninfer_executable", "ninfer_artifact", "ninfer_max_context", "ninfer_max_concurrency",
               "ninfer_kv_dtype", "ninfer_kv_capacity", "ninfer_host_kv_mib", "ninfer_speed_preset",
               "ninfer_fast_prefill_kernel")


def settings_key(app) -> tuple:
    """What a start would run with: the engine's name and every setting that changes its argv."""
    backend = getattr(app, "backend", None)
    s = getattr(backend, "_settings", None) or getattr(app, "settings", None)
    return (getattr(backend, "name", ""), *(str(getattr(s, field, None)) for field in _KEY_FIELDS))


def remember_failure(app, error) -> None:
    """Record a start that failed for a reason these settings will repeat; forget it otherwise."""
    reason = getattr(error, "reason", None)
    app._engine_start_blocked = (settings_key(app), str(error)) if reason else None


def forget_failure(app) -> None:
    app._engine_start_blocked = None


def declined_words(app, source: str) -> str | None:
    """The sentence to show INSTEAD of starting, or None. Only a person's send is declined, and
    only while nothing serves and the settings are the ones that failed."""
    blocked = getattr(app, "_engine_start_blocked", None)
    if not blocked or blocked[0] != settings_key(app):
        return None
    if waiting(app) or not (nothing_serving(app) and at_keyboard(app, source)):
        return None
    return (f"{blocked[1]} Not starting it again with the same settings: change them, "
            "or /engine start to try anyway.")


def decline(app, item: dict, words: str) -> None:
    """Say why nothing started, and give the message back."""
    app.system_message(words)
    give_back(app, [item], queued=False)


def begin(app) -> None:
    """Mark a start as waited on, so later sends queue behind it. Idempotent."""
    app._engine_autostart = True
    if getattr(app, "_engine_autostart_held", None) is None:
        app._engine_autostart_held = []


def settle(app, backend) -> bool:
    """End the wait: send what was held if the engine now serves, else give it back.
    Idempotent and synchronous, so a `finally` can always call it. True when it serves."""
    if not waiting(app):
        return False
    app._engine_autostart = False
    held = getattr(app, "_engine_autostart_held", None) or []
    app._engine_autostart_held = []
    if not held:
        return False
    served = app.backend is backend and not nothing_serving(app)
    if served:
        app._flush_pending_input()
    else:
        give_back(app, held)
    return served


def hold(app, item: dict) -> None:
    """Queue `item` (a `_pending_input` row); start the engine only if no start is running."""
    app._pending_input.append(item)
    held = getattr(app, "_engine_autostart_held", None)
    if held is None:
        held = app._engine_autostart_held = []
    held.append(item)
    if waiting(app):
        app.notify("Queued — sends once the engine is ready", timeout=3)
        return
    begin(app)
    backend = app.backend
    label = getattr(backend, "label", backend.name)
    if getattr(backend, "_starting", False):
        app.system_message(f"A {label} start is already under way — your message sends once it has loaded.")
    else:
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
    """The waiter a held send starts. Three cases, and only the last one starts anything:
    a start already in flight (not ours) -> wait for it, reconnect, send; an engine already
    answering that this app is not connected to -> attach and send; nothing -> start."""
    import asyncio

    from .plugins.model_switch import await_reconnect, start_and_connect

    try:
        if getattr(backend, "_starting", False):
            while getattr(backend, "_starting", False):
                await asyncio.sleep(0.5)
            await await_reconnect(app.connect())
            return
        try:
            await backend.ensure_running()        # read-only: is one answering already?
        except Exception:  # noqa: BLE001 - not answering (or cannot tell): start one
            pass
        else:
            app.system_message(f"A {getattr(backend, 'label', backend.name)} engine is already answering "
                               "— connecting to it instead of starting one.")
            await await_reconnect(app.connect())
            return
        await start_and_connect(app, backend)
    finally:
        settle(app, backend)


def give_back(app, held: list[dict], *, queued: bool = True) -> None:
    """A start that did not serve: take the held messages off the queue (so nothing sends them
    to an engine that is not there) and put their text back in the input box. `queued=False`:
    the messages never reached the queue (a send declined before it), so there is nothing to remove."""
    texts = []
    for item in held:
        if queued:
            try:
                app._pending_input.remove(item)
            except ValueError:
                continue                    # already sent or dropped elsewhere
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
