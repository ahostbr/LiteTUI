from types import SimpleNamespace

import pytest
from textual.app import App

from litetui import voice_backend
from litetui.response_speech import ResponseSpeakButton
from litetui.widgets import AssistantMessage


@pytest.mark.asyncio
async def test_each_response_speaks_own_text_and_stops(monkeypatch):
    active = set()
    spoken = []
    monkeypatch.setattr(voice_backend, 'is_playing', lambda owner: owner in active)
    def stop(owner=None):
        active.clear() if owner is None else active.discard(owner)
    def speak(text, **kw):
        spoken.append(text)
        active.add(kw['owner'])
        return True
    monkeypatch.setattr(voice_backend, 'stop', stop)
    monkeypatch.setattr(voice_backend, 'speak', speak)
    class Host(App):
        CSS = 'AssistantMessage { height: auto; }'
        settings = SimpleNamespace(tts_engine='edge', tts_edge_voice='voice', tts_voice='', tts_timeout=30)
        def compose(self):
            for text in ['first response', 'second response']:
                card = AssistantMessage()
                card.set_answer(text)
                yield card
    async with Host().run_test() as pilot:
        buttons = list(pilot.app.query(ResponseSpeakButton))
        assert not spoken  # rendering never auto-speaks
        await pilot.click(buttons[0])
        assert spoken == ['first response']
        assert buttons[0] in active
        await pilot.click(buttons[0])
        assert not active
        await pilot.click(buttons[1])
        assert spoken[-1] == 'second response'
        assert buttons[1] in active
    assert not active


def test_prompt_has_no_speak_toggle():
    import inspect

    from litetui.input_controls import PromptBox
    assert 'SpeakButton' not in inspect.getsource(PromptBox.compose)


@pytest.mark.asyncio
async def test_idle_speech_button_emits_nothing_but_state_changes_render(monkeypatch):
    from textual.drivers.headless_driver import HeadlessDriver

    active = set()
    monkeypatch.setattr(voice_backend, 'is_playing', lambda owner: owner in active)
    monkeypatch.setattr(voice_backend, 'stop', lambda owner: active.discard(owner))

    class CaptureDriver(HeadlessDriver):
        @property
        def is_headless(self):
            return False

        def write(self, data):
            self._app.writes.append(data)

    class Host(App):
        def __init__(self):
            self.writes = []
            self.settings = SimpleNamespace(tts_enabled=True)
            self.response = SimpleNamespace(answer_text='hello')
            super().__init__(driver_class=CaptureDriver)

        def compose(self):
            yield ResponseSpeakButton(self.response)

    app = Host()
    async with app.run_test(headless=False) as pilot:
        await pilot.pause(.3)
        button = app.query_one(ResponseSpeakButton)
        app.writes.clear()
        await pilot.pause(.6)  # more than two playback polls
        assert app.writes == []
        active.add(button)
        await pilot.pause(.3)
        assert '■ Stop' in ''.join(app.writes)
        app.writes.clear()
        active.clear()  # playback ends externally, not via a click
        await pilot.pause(.3)
        assert '♫ Speak' in ''.join(app.writes)
        app.settings.tts_enabled = False
        await pilot.pause(.3)
        assert not button.display
        app.settings.tts_enabled = True
        app.response.answer_text = ''
        await pilot.pause(.3)
        assert not button.display
        app.response.answer_text = 'new answer'
        await pilot.pause(.3)
        assert button.display
