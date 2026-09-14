"""Read-only pending-question replay; reopening requires an explicit user action."""

from textual import on
from textual.widgets import Button

from litetui.widgets import FoldBlock


class SavedQuestionCard(FoldBlock):
    def __init__(self, metadata, entry):
        self.metadata, self.entry = metadata, entry
        self.manager = None
        super().__init__(
            "Unanswered Codex question",
            "\n\n".join(question["title"] for question in entry["questions"]),
            expanded=True,
        )
        self.answer = Button("Answer question", classes="codex-answer-saved")

    def compose(self):
        yield from super().compose()
        yield self.answer

    def on_mount(self):
        self.set_interval(0.2, self.refresh_state)

    def refresh_state(self):
        state = self.entry.get("state")
        if state != "pending":
            self.answer.label = (
                "Answer queued" if state == "answered" else "Question closed"
            )
            self.answer.disabled = True
        elif self.manager is not None and self.entry["id"] not in self.manager.active:
            self.answer.label = "Answer question"
            self.answer.disabled = False

    @on(Button.Pressed, ".codex-answer-saved")
    def answer_saved(self, event):
        event.stop()
        app = self.app
        if not hasattr(app.backend, "app_server"):
            app.notify(
                "Reconnect the Codex backend to answer this question.",
                severity="warning",
            )
            return
        index = next(
            (
                index
                for index, message in enumerate(app.conversation)
                if message.get("provider_metadata") is self.metadata
            ),
            None,
        )
        if index is None or self.entry.get("conversationId") != app.convo_id:
            app.notify(
                "This question belongs to another conversation.", severity="warning"
            )
            return
        from litetui.codex_async_questions import AsyncQuestions
        from litetui.model_transport import for_app

        transport = for_app(app)
        manager = getattr(transport.server, "async_questions", None)
        if manager is None:
            manager = AsyncQuestions(transport)
        self.manager = manager
        manager.open_saved(self.entry, self.metadata, index)
        self.answer.label = "Question reopened"
        self.answer.disabled = True
