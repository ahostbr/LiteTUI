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


class PromptBox(Container):
    DEFAULT_CSS = '''
    PromptBox { width: 100%; height: 5; margin: 0 1 1 1; }
    PromptBox #message-input { position: absolute; offset: 0 0; width: 100%; height: 5; margin: 0 !important; padding-bottom: 1; }
    PromptBox #scroll-lock { position: absolute; offset: 0 0; width: 4; height: 1; background: $surface; }
    PromptBox #prompt-actions { position: absolute; offset: 0 4; width: 17; height: 1; background: $surface; }
    PromptBox #prompt-actions > Static { width: auto; height: 1; padding: 0 !important; }
    PromptBox #speak-toggle.enabled { color: $success; }
    PromptBox #scroll-lock:hover, PromptBox #prompt-actions > Static:hover { background: $primary 40%; }
    '''

    def on_mount(self):
        self.query_one('#message-input').styles.margin = 0
        for button in self.query('#prompt-actions > Static'):
            button.styles.padding = 0
        self.call_after_refresh(self.sync_compact)

    def on_resize(self):
        self.sync_compact()

    def on_descendant_focus(self, _event):
        self.sync_compact()

    def on_descendant_blur(self, _event):
        self.call_after_refresh(self.sync_compact)

    def sync_compact(self):
        field = self.query_one('#message-input', PromptInput)
        app = self.app
        narrow = getattr(app, '_compact_mode', False) and not (
            app.screen.focused is field or bool(field.value))
        self.set_class(narrow, 'slim-prompt')
        # Input has a three-cell intrinsic minimum: top border, one text row,
        # bottom border. Two cells clip the bottom edge and its controls.
        self.styles.height = 3 if narrow else 5
        field.styles.height = 3 if narrow else 5
        field.styles.padding_bottom = 0 if narrow else 1
        field.styles.border_bottom = None
        self.query_one('#scroll-lock').styles.offset = (max(0, self.size.width - 6), 0)
        self.query_one('#prompt-actions').styles.offset = (
            max(0, self.size.width - 19), 1 if narrow else 4)

    def compose(self):
        yield PromptInput(placeholder='Message... (Ctrl+V paste | Ctrl+O image | /help)', id='message-input')
        yield ScrollLockButton()
        with Horizontal(id='prompt-actions'):
            yield MicButton()
            yield PauseButton()
