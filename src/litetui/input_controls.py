"""Clickable controls embedded in the prompt's border rows."""
from textual.containers import Container, Horizontal
from textual.widgets import Static
from litetui.widgets import PromptInput, MicButton, PauseButton


class ScrollLockButton(Static):
    def __init__(self):
        super().__init__(' 🔒 ', id='scroll-lock')
        self.tooltip = 'Locked: follow newest output. Click to scroll freely.'

    def on_click(self):
        self.app.action_toggle_scroll_lock()


class SpeakButton(Static):
    def __init__(self):
        super().__init__(' ♫ speak ', id='speak-toggle')

    def on_click(self):
        self.app.action_toggle_speak()


class PromptBox(Container):
    DEFAULT_CSS = '''
    PromptBox { height: 5; margin: 0 1 1 1; }
    PromptBox #message-input { position: absolute; offset: 0 0; width: 100%; height: 5; margin: 0 !important; padding-bottom: 1; }
    PromptBox #scroll-lock { position: absolute; offset: 0 0; width: 4; height: 1; background: $surface; }
    PromptBox #prompt-actions { position: absolute; offset: 0 4; width: 27; height: 1; background: $surface; }
    PromptBox #prompt-actions > Static { width: auto; height: 1; padding: 0 !important; }
    PromptBox #speak-toggle.enabled { color: $success; }
    PromptBox #scroll-lock:hover, PromptBox #prompt-actions > Static:hover { background: $primary 40%; }
    '''

    def on_resize(self):
        self.query_one('#scroll-lock').styles.offset = (max(0, self.size.width - 6), 0)
        self.query_one('#prompt-actions').styles.offset = (max(0, self.size.width - 29), 4)

    def compose(self):
        yield PromptInput(placeholder='Message... (Ctrl+V paste | Ctrl+O image | /help)', id='message-input')
        yield ScrollLockButton()
        with Horizontal(id='prompt-actions'):
            yield MicButton()
            yield SpeakButton()
            yield PauseButton()
