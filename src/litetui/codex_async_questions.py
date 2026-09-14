"""Native async questions: shared human UI, durable ordinary-user-message replies."""

import asyncio
import json
import uuid
from threading import Event

from litetui.codex_steering import SteeringLedger, save_at
from litetui.model_transport import ProviderError
from litetui.question_result import capture_answers, question_lifetime, question_slot
from litetui.tool_events import native_lifecycle


def answer_text(questions, payload):
    """Only explicitly submitted answers become a user message."""
    if payload.get("action") != "submit":
        return None
    lines = []
    for question, answer in zip(questions, payload.get("questions", [])):
        if not answer.get("answered"):
            continue
        options = answer.get("options", [])
        selected = [
            str(options[index]["title"])
            for index in answer.get("selected", [])
            if isinstance(index, int) and 0 <= index < len(options)
        ]
        if note := str(answer.get("note") or "").strip():
            selected.append(note)
        if selected:
            lines.append(
                f"Question: {question['title']}\nAnswer: " + "\n".join(selected)
            )
    return "\n\n".join(lines) or None


class AsyncQuestions:
    def __init__(self, transport):
        self.transport = transport
        self.active = {}
        transport.server.async_questions = self

    def cancel(self):
        for cancelled, _ in self.active.values():
            cancelled.set()

    def open(self, item, metadata, index):
        app = self.transport.app
        if app is None or index is None or not item.get("questions"):
            return
        conversation_id = getattr(app, "convo_id", None)
        thread_id, turn_id = self.transport.thread_id, self.transport.turn_id
        # The native schema permits more questions than the shared widget's cap.
        # Each explicit batch submit is its own user reply, never an inferred answer.
        for offset in range(0, len(item["questions"]), 8):
            key = json.dumps(
                [thread_id, turn_id, item["id"], offset], separators=(",", ":")
            )
            entries = metadata.setdefault("async_questions", [])
            entry = next((entry for entry in entries if entry["id"] == key), None)
            if entry is None:
                entry = {
                    "version": 1,
                    "id": key,
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": item["id"],
                    "conversationId": conversation_id,
                    "questions": item["questions"][offset : offset + 8],
                    "state": "pending",
                }
                entries.append(entry)
                save_at(app, index)
            if key in self.active or entry["state"] != "pending":
                continue
            self.open_saved(entry, metadata, index)

    def open_saved(self, entry, metadata, index):
        if entry["id"] in self.active or entry["state"] != "pending":
            return
        app = self.transport.app
        cancelled = Event()
        worker = (
            app.run_worker(
                self._run(entry, metadata, index, cancelled),
                group="codex-async-questions",
                exclusive=False,
                exit_on_error=False,
            )
            if hasattr(app, "run_worker")
            else asyncio.create_task(self._run(entry, metadata, index, cancelled))
        )
        self.active[entry["id"]] = (cancelled, worker)

    async def _run(self, entry, metadata, index, cancelled):
        app = self.transport.app
        question_task = None
        monitor = None

        async def ask():
            with (
                question_lifetime(cancelled),
                capture_answers() as captured,
                native_lifecycle(),
            ):
                async with question_slot(app):
                    _, success = await app._execute_tool(
                        "ask_user_question",
                        {
                            "questions": [
                                {
                                    "label": "Codex question",
                                    "question": question["title"],
                                    "multiSelect": False,
                                    "allowFreeText": True,
                                    "options": [
                                        {"title": option}
                                        for option in question.get("options") or []
                                    ],
                                }
                                for question in entry["questions"]
                            ],
                        },
                    )
                return success, captured[-1] if captured else {}

        async def watch():
            stop_armed = not getattr(app, "_stop_requested", False)
            while not question_task.done():
                if not getattr(app, "_stop_requested", False):
                    stop_armed = True
                if (
                    getattr(app, "convo_id", None) != entry["conversationId"]
                    or not getattr(app, "is_running", True)
                    or (stop_armed and getattr(app, "_stop_requested", False))
                ):
                    cancelled.set()
                    return
                await asyncio.sleep(0.1)

        try:
            question_task = asyncio.create_task(ask())
            monitor = asyncio.create_task(watch())
            success, payload = await question_task
            if cancelled.is_set():
                return  # keep the persisted question pending, never invent an answer
            if getattr(app, "convo_id", None) != entry["conversationId"]:
                return
            text = answer_text(entry["questions"], payload) if success else None
            if text is None:
                entry["state"] = payload.get("action") or "unanswered"
                save_at(app, index)
                return
            self.queue_answer(entry, metadata, index, text)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception:  # noqa: BLE001 - report without logging private answers
            message = "Codex question answer was not delivered. Its saved state must be checked before retrying."
            app._rpc_emit({"type": "error", "error": message})
            if not getattr(app, "_rpc", False) and hasattr(app, "_system"):
                app._system(message)
        finally:
            cancelled.set()
            if monitor is not None:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
            if question_task is not None and not question_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(question_task), 1)
                except (TimeoutError, asyncio.CancelledError):
                    question_task.cancel()
                await asyncio.gather(question_task, return_exceptions=True)
            self.active.pop(entry["id"], None)

    def queue_answer(self, entry, metadata, index, text):
        app = self.transport.app
        if app.conversation[index].get("provider_metadata") is not metadata:
            raise ProviderError(
                "The question's conversation changed before its answer was saved."
            )
        ident = str(
            uuid.uuid5(uuid.NAMESPACE_URL, "litetui-codex-question:" + entry["id"])
        )
        entries = metadata.setdefault("steering", [])
        delivery = next((value for value in entries if value["id"] == ident), None)
        if delivery is None:
            item = {
                "content": text,
                "text": text,
                "source": "codex-question",
                "tool_profile": getattr(app, "chosen_tool_profile", "interactive"),
            }
            delivery = {
                "id": ident,
                "threadId": entry["threadId"],
                "turnId": entry["turnId"],
                "conversationId": entry["conversationId"],
                "state": "queued",
                "item": item,
                "instructions_digest": metadata.get("instructions_digest"),
            }
            entries.append(delivery)
        entry.update(state="answered", deliveryId=ident)
        # Persist question outcome and queue entry together BEFORE queue visibility.
        save_at(app, index)
        if delivery["state"] in ("accepted", "denied") or any(
            message.get("codex_delivery", {}).get("id") == ident
            for message in app.conversation
        ):
            return
        ledger = SteeringLedger(entries, lambda: save_at(app, index))
        if not any(
            value.get("_codex_entry", {}).get("id") == ident
            for value in app._pending_input
        ):
            app._pending_input.append(
                {**delivery["item"], "_codex_entry": delivery, "_codex_ledger": ledger}
            )
            if hasattr(app, "_user_bubble"):
                app._user_bubble(text, False, queued=True)
        if hasattr(app, "call_after_refresh"):
            app.call_after_refresh(app._flush_pending_input)
