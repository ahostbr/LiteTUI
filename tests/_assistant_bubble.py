"""Minimal assistant widget used by source and packaged-process tests."""


class FakeBody:
    def __init__(self):
        self.content = ""

    def set_markdown(self, src):
        self.content = src


class FakeBubble:
    def __init__(self):
        self.body = FakeBody()
        self.answer = None
        self.settled = False
        self.thinking = None
        self.mounted = []
        self.is_attached = True

    def set_answer(self, text):
        self.answer = text

    def mount(self, child, before=None):
        self.mounted.append(child)

    def set_thinking(self, block):
        """AssistantMessage.set_thinking's contract (T1031): replace, then
        remove the block it replaced."""
        old, self.thinking = self.thinking, block
        self.mounted.append(block)
        if old is not None and old is not block:
            old.remove()
