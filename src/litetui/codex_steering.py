"""Durable delivery states for native steering; never retry an uncertain send."""

import asyncio
import copy
import uuid

from litetui.model_transport import ProviderError


class RejectedRequest(ProviderError):
    """An explicit JSON-RPC rejection, distinct from a lost connection/reply."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def save_at(app, index):
    app._edit(index, "Codex queued delivery updated")
    store = getattr(app, "store", None)
    if store is not None and (
        store.persist_error or store.convo_path is None or store.loading
    ):
        raise ProviderError(
            "Codex input remains queued because its delivery state could not be saved."
        )


class SteeringLedger:
    """Lives in private conversation metadata, never in operational logs.

    Every transition is saved before the next external side effect. The host
    supplies persistence and admission; native history supplies acceptance proof.
    ``next_turn`` means an explicit rejection permits fallback, not a resend to
    the same active turn. ``uncertain`` requires reconciliation first.
    """

    def __init__(self, entries, save):
        self.entries = entries
        self.save = save

    def enqueue(self, item, thread_id, turn_id):
        entry = {
            "id": str(uuid.uuid4()),
            "threadId": thread_id,
            "turnId": turn_id,
            "state": "queued",
            "item": {
                key: copy.deepcopy(item[key])
                for key in ("content", "text", "source", "tool_profile", "operation_id")
                if key in item
            },
        }
        self.entries.append(entry)
        self.save()
        return entry

    def transition(self, entry, state, **values):
        entry.update(values)
        entry["state"] = state
        entry["revision"] = entry.get("revision", 0) + 1
        self.save()

    async def deliver(self, entry, *, admit, request):
        if entry["state"] == "queued":
            # An interrupted admission is not blindly run a second time.
            self.transition(entry, "admitting")
            allowed, context = await admit(entry["item"])
            self.transition(
                entry, "admitted" if allowed else "denied", admission=context
            )
        if entry["state"] != "admitted":
            return entry["state"]
        from litetui.codex_app_server import user_input

        params = {
            "threadId": entry["threadId"],
            "expectedTurnId": entry["turnId"],
            "clientUserMessageId": entry["id"],
            "input": user_input(
                [{"role": "user", "content": entry["item"]["content"]}]
            ),
        }
        self.transition(entry, "sending")
        try:
            reply = await request("turn/steer", params)
        except RejectedRequest as error:
            # Invalid request/params are rejected before native input injection.
            # Internal errors cannot prove that no side effect occurred.
            self.transition(
                entry, "next_turn" if error.code in (-32600, -32602) else "uncertain"
            )
        except (ProviderError, TimeoutError, OSError):
            self.transition(entry, "uncertain")
        else:
            self.transition(
                entry,
                "accepted" if reply.get("turnId") == entry["turnId"] else "uncertain",
            )
        return entry["state"]

    def reconcile(self, entry, history):
        thread = history.get("thread") or {}
        if thread.get("id") != entry["threadId"]:
            return False
        for turn in thread.get("turns", []):
            if turn.get("id") != entry["turnId"]:
                continue
            if any(
                item.get("type") == "userMessage"
                and item.get("clientId") == entry["id"]
                for item in turn.get("items", [])
            ):
                self.transition(entry, "accepted")
                return True
        # An absent item in a partial, compacted or rolled-back history is not
        # proof that input was never accepted. Preserve the uncertain delivery.
        return False


class HostSteering:
    def __init__(self, app, server, thread_id, turn_id, metadata, save):
        self.app, self.server = app, server
        self.thread_id, self.turn_id = thread_id, turn_id
        self.metadata = metadata
        self.ledger = SteeringLedger(metadata.setdefault("steering", []), save)

    async def admit(self, item):
        from litetui import hook_host

        context = {
            **hook_host.context(self.app),
            "source": item.get("source", "queued"),
            "turn_id": str(uuid.uuid4()),
        }
        if getattr(self.app, "hook_config", None) is None:
            return True, context
        await hook_host.drain_lifecycle(self.app)
        result = await hook_host.dispatch(
            self.app,
            "prompt_before",
            {"prompt": item["content"]},
            profile=item.get("tool_profile"),
            captured=context,
        )
        context["reason"] = result.reason
        return result.allowed and not self.app._stop_requested, context

    async def run(self):
        while not getattr(self.app, "_stop_requested", False):
            queue = getattr(self.app, "_pending_input", [])
            if not queue:
                await asyncio.sleep(0.1)
                continue
            item = queue[0]
            if item.get("source") == "interrupted":
                return
            entry = item.get("_codex_entry")
            if entry is None:
                entry = self.ledger.enqueue(item, self.thread_id, self.turn_id)
                entry["conversationId"] = getattr(self.app, "convo_id", None)
                entry["instructions_digest"] = self.metadata.get("instructions_digest")
                self.ledger.save()
                item["_codex_entry"] = entry
                item["_codex_ledger"] = self.ledger
            ledger = item.get("_codex_ledger", self.ledger)
            if entry["threadId"] != self.thread_id:
                return
            state = await ledger.deliver(
                entry, admit=self.admit, request=self.server.request
            )
            if state == "accepted":
                accept_steered(self.app, item)
            elif state == "denied":
                self.app.rejected_prompts.append(
                    {
                        **entry["item"],
                        "reason": entry["admission"].get("reason", "Prompt refused"),
                    }
                )
                self.app._system(
                    "Queued prompt rejected by its admission hook; retained in /hooks."
                )
                remove_item(queue, item)
            else:
                # Known rejection uses the ordinary next-turn path. Uncertain
                # sends remain held until native history confirms acceptance.
                return


def remove_item(queue, item):
    for index, value in enumerate(queue):
        if value is item:
            queue.pop(index)
            break


def accept_steered(app, item):
    from litetui import hook_host
    from litetui.widgets import _mark_delivered

    entry = item["_codex_entry"]
    if entry.get("conversationId") not in (None, getattr(app, "convo_id", None)):
        raise ProviderError(
            "Queued input belongs to another conversation and remains retained."
        )
    if not any(
        m.get("codex_delivery", {}).get("id") == entry["id"] for m in app.conversation
    ):
        hook_host.accept_prompt(
            app,
            {
                **item,
                "_gui_in_turn": True,
                "_codex_metadata": {
                    "provider": "codex",
                    "app_server_thread_id": entry["threadId"],
                    "instructions_digest": entry.get("instructions_digest"),
                },
            },
        )
        app._hook_turn_id = entry.get("admission", {}).get("turn_id", app._hook_turn_id)
    _mark_delivered(item)
    remove_item(app._pending_input, item)


def restore_queue(app):
    """Restore journaled input without dispatching tools or sending model input."""
    found = {}
    materialized = {m.get("codex_delivery", {}).get("id") for m in app.conversation}
    queued = {i.get("_codex_entry", {}).get("id") for i in app._pending_input}
    for index, message in enumerate(app.conversation):
        entries = (message.get("provider_metadata") or {}).get("steering", [])
        for entry in entries:
            previous = found.get(entry.get("id"))
            if previous is None or entry.get("revision", 0) >= previous[0].get(
                "revision", 0
            ):
                found[entry.get("id")] = (entry, entries, index)
    for ident, (entry, entries, index) in found.items():
        if (
            not ident
            or ident in materialized
            or ident in queued
            or entry.get("state") == "denied"
        ):
            continue
        ledger = SteeringLedger(
            entries,
            lambda index=index: save_at(app, index),
        )
        item = {**entry["item"], "_codex_entry": entry, "_codex_ledger": ledger}
        app._pending_input.append(item)


async def recover_queue_head(app, server):
    """Positive identity proof only; missing native history leaves input held."""
    if not app._pending_input:
        return False
    item = app._pending_input[0]
    entry = item.get("_codex_entry")
    if not entry:
        return False
    if entry.get("conversationId") not in (None, getattr(app, "convo_id", None)):
        return False
    if entry["state"] != "accepted":
        try:
            await server.start()
            history = await server.request(
                "thread/read", {"threadId": entry["threadId"], "includeTurns": True}
            )
        except (ProviderError, TimeoutError, OSError):
            return False
        if not item["_codex_ledger"].reconcile(entry, history):
            return False
    accept_steered(app, item)
    return True


def message_state(app, message, state):
    message["codex_delivery"]["state"] = state
    if app is not None:
        if state == "accepted":
            from litetui.widgets import _mark_delivered

            bubble = getattr(app, "_codex_delivery_bubbles", {}).pop(
                message["codex_delivery"]["id"], None
            )
            _mark_delivered({"bubble": bubble})
        for index, original in enumerate(app.conversation):
            if original is message:
                save_at(app, index)
                break


async def reconcile_messages(app, server, messages):
    for message in messages:
        delivery = message.get("codex_delivery") or {}
        if delivery.get("state") != "sending":
            continue
        history = await server.request(
            "thread/read", {"threadId": delivery["threadId"], "includeTurns": True}
        )
        accepted = history.get("thread", {}).get("id") == delivery["threadId"] and any(
            item.get("type") == "userMessage" and item.get("clientId") == delivery["id"]
            for turn in history.get("thread", {}).get("turns", [])
            for item in turn.get("items", [])
        )
        if not accepted:
            raise ProviderError(
                "A queued Codex input has an uncertain delivery. It is retained; native history has not confirmed acceptance yet."
            )
        message["provider_metadata"] = {
            "provider": "codex",
            "app_server_thread_id": delivery["threadId"],
        }
        message_state(app, message, "accepted")
