"""Explicit, response-owned speech; never automatic turn playback."""
from textual.widgets import Static

from litetui import voice_backend


class ResponseSpeakButton(Static):
    DEFAULT_CSS = '''
    ResponseSpeakButton { width: 100%; height: 1; text-align: right; color: $text-muted; }
    ResponseSpeakButton:hover { color: $accent; }
    '''

    def __init__(self, response):
        super().__init__('♫ Speak', classes='response-speak')
        self.response = response
        self.tooltip = 'Read this response aloud; click again to stop'

    def on_mount(self):
        self.set_interval(0.25, self.refresh_playback)

    def refresh_playback(self):
        label = '■ Stop' if voice_backend.is_playing(self) else '♫ Speak'
        # Static.update repaints even identical content. Keep polling for external
        # playback completion, but render only when the visible label changes.
        if self.content != label:
            self.update(label)
        # tts_enabled is the show/hide switch for these buttons (Ryan 2026-09-24).
        shown = getattr(getattr(self.app, 'settings', None), 'tts_enabled', True)
        self.display = bool(shown and self.response.answer_text.strip())

    def on_click(self, event):
        event.stop()
        if voice_backend.is_playing(self):
            voice_backend.stop(self)
        else:
            text = self.response.answer_text
            if not text.strip():
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
