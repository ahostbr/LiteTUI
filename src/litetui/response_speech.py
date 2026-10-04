"""Explicit, response-owned speech; never automatic turn playback."""
from textual.message import Message
from textual.widgets import Static

from litetui import voice_backend


class ResponseSpeakButton(Static):
    DEFAULT_CSS = '''
    ResponseSpeakButton { width: 100%; height: 1; text-align: right; color: $text-muted; }
    ResponseSpeakButton:hover { color: $accent; }
    '''

    class StateChanged(Message):
        """The response's header speech affordance needs refreshing."""

    def __init__(self, response):
        super().__init__('♫ Speak', classes='response-speak')
        self.response = response
        self.header_label = ''
        self.tooltip = 'Read this response aloud; click again to stop'

    def on_mount(self):
        self.refresh_playback()
        self.set_interval(0.25, self.refresh_playback)

    def _speech_text(self) -> str:
        """Prefer the answer, then the full recap over the clipped card summary."""
        for text in (
            self.response.answer_text,
            getattr(self.response, 'recap', None),
            getattr(self.response, 'summary', None),
        ):
            if text and text.strip():
                return text
        return ''

    def refresh_playback(self):
        label = '■ Stop' if voice_backend.is_playing(self) else '♫ Speak'
        # Static.update repaints even identical content. Keep polling for external
        # playback completion, but render only when the visible label changes.
        if self.content != label:
            self.update(label)
        # tts_enabled is the show/hide switch for these buttons (the user 2026-09-24).
        shown = getattr(getattr(self.app, 'settings', None), 'tts_enabled', True)
        available = bool(shown and self._speech_text())
        self.display = available and not getattr(self.response, 'collapsed', False)
        header_label = label if available else ''
        if self.header_label != header_label:
            self.header_label = header_label
            self.post_message(self.StateChanged())

    def on_click(self, event):
        event.stop()
        self.toggle_playback()

    def toggle_playback(self):
        """Handle an explicit click from the body button or folded header."""
        self.refresh_playback()
        if not self.header_label:
            return
        if voice_backend.is_playing(self):
            voice_backend.stop(self)
        else:
            text = self._speech_text()
            if not text:
                return
            settings = self.app.settings
            # Only one response at a time, without affecting another App process.
            voice_backend.stop()
            ok = voice_backend.speak(text, engine=settings.tts_engine,
                voice=(settings.tts_edge_voice if settings.tts_engine == 'edge' else settings.tts_voice) or None,
                timeout=settings.tts_timeout, owner=self)
            if not ok:
                self.tooltip = 'Speech unavailable — check Voice settings and installed engine'
        self.refresh_playback()

    def on_unmount(self):
        voice_backend.stop(self)
