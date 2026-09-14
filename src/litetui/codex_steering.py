"""Durable delivery states for native steering; never retry an uncertain send."""

import copy
import uuid

from litetui.model_transport import ProviderError


class RejectedRequest(ProviderError):
    """An explicit JSON-RPC rejection, distinct from a lost connection/reply."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


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
