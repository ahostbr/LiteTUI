from types import SimpleNamespace
import pytest
from textual.app import App
from litetui.widgets import AssistantMessage
from litetui.response_speech import ResponseSpeakButton
from litetui import voice_backend


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
