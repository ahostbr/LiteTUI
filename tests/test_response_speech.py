from types import SimpleNamespace

import pytest
from textual.app import App

from litetui import voice_backend
from litetui.recap import split_recap
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


@pytest.mark.asyncio
@pytest.mark.parametrize(('answer', 'recap', 'summary', 'expected'), [
    pytest.param('', 'Did work / Tests green', None, 'Did work / Tests green',
                 id='recap-only'),
    pytest.param(' \n\t', 'Did work / Tests green', None, 'Did work / Tests green',
                 id='blank-answer'),
    pytest.param('', None, 'A generated summary', 'A generated summary',
                 id='summary-only'),
    pytest.param(' \t', ' \n', 'A generated summary', 'A generated summary',
                 id='blank-recap'),
    pytest.param('Full answer', 'Short recap', 'Short summary', 'Full answer',
                 id='answer-wins'),
    pytest.param('', 'Completed the requested change. ' * 4, None,
                 'Completed the requested change. ' * 4, id='unclipped-recap'),
    pytest.param('', None, None, '', id='empty'),
    pytest.param(' \n', ' \t', ' \n', '', id='all-blank'),
])
async def test_speech_text_fallback_and_visibility(monkeypatch, answer, recap, summary, expected):
    spoken = []
    monkeypatch.setattr(voice_backend, 'is_playing', lambda owner: False)
    monkeypatch.setattr(voice_backend, 'stop', lambda owner=None: None)
    monkeypatch.setattr(voice_backend, 'speak',
                        lambda text, **kw: spoken.append((text, kw)) or True)

    class Host(App):
        CSS = 'AssistantMessage { height: auto; }'
        settings = SimpleNamespace(tts_enabled=True, tts_engine='edge',
                                   tts_edge_voice='voice', tts_voice='', tts_timeout=30)

        def compose(self):
            card = AssistantMessage()
            card.set_answer(answer)
            card.recap = recap
            card.set_summary(summary if summary is not None else recap)
            yield card

    async with Host().run_test() as pilot:
        button = pilot.app.query_one(ResponseSpeakButton)
        button.refresh_playback()
        assert button.display is bool(expected)
        assert not spoken  # rendering never auto-speaks
        if expected:
            assert await pilot.click(button)
            assert spoken == [(expected, {
                'engine': 'edge', 'voice': 'voice', 'timeout': 30, 'owner': button,
            })]
        else:
            # A stale/queued click must not try to speak a blank response.
            button.on_click(SimpleNamespace(stop=lambda: None))
            assert not spoken
        pilot.app.settings.tts_enabled = False
        button.refresh_playback()
        assert not button.display
        pilot.app.settings.tts_enabled = True
        button.refresh_playback()
        assert button.display is bool(expected)


@pytest.mark.asyncio
async def test_recap_only_reply_projection_has_a_working_speak_button(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice_backend, 'is_playing', lambda owner: False)
    monkeypatch.setattr(voice_backend, 'stop', lambda owner=None: None)
    monkeypatch.setattr(voice_backend, 'speak', lambda text, **kw: spoken.append(text) or True)

    class Host(App):
        CSS = 'AssistantMessage { height: auto; }'
        settings = SimpleNamespace(tts_enabled=True, tts_engine='edge',
                                   tts_edge_voice='voice', tts_voice='', tts_timeout=30)

        def compose(self):
            answer, recap = split_recap('<recap>Did work\nTests green</recap>', final=True)
            card = AssistantMessage()
            card.set_answer(answer)
            card.recap = recap
            card.set_summary(recap)
            yield card

    async with Host().run_test() as pilot:
        card = pilot.app.query_one(AssistantMessage)
        assert card.answer_text == ''
        button = card.query_one(ResponseSpeakButton)
        button.refresh_playback()
        assert button.display
        assert not spoken
        assert await pilot.click(button)
        assert spoken == ['Did work / Tests green']


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


@pytest.fixture
def folded_speech_host(monkeypatch):
    from litetui.app import LiteTUI
    from litetui import themes

    active, spoken = set(), []
    monkeypatch.setattr(voice_backend, 'is_playing', lambda owner: owner in active)

    def stop(owner=None):
        active.clear() if owner is None else active.discard(owner)

    def speak(text, **kwargs):
        spoken.append((text, kwargs['owner']))
        active.add(kwargs['owner'])
        return True

    monkeypatch.setattr(voice_backend, 'stop', stop)
    monkeypatch.setattr(voice_backend, 'speak', speak)

    class Host(App):
        CSS = LiteTUI.CSS

        def __init__(self, *cards, **kwargs):
            super().__init__(**kwargs)
            self.cards = cards
            self.settings = SimpleNamespace(tts_enabled=True, tts_engine='edge',
                                            tts_edge_voice='voice', tts_voice='', tts_timeout=30)

        def get_css_variables(self):
            variables = super().get_css_variables()
            for key, value in themes.extra_defaults(
                primary=variables['primary'], bone=variables['foreground'],
            ).items():
                variables.setdefault(key, value)
            return variables

        def compose(self):
            yield from self.cards

    return Host, active, spoken


def _header_row(card):
    from textual.geometry import Region

    return card.render_lines(Region(0, 0, card.region.width, 1))[0].text


async def _click_header(pilot, card, label):
    from rich.cells import cell_len

    row = _header_row(card)
    assert label in row
    # Click rendered text, not private event handlers or guessed coordinates.
    x = cell_len(row[:row.index(label)])
    assert await pilot.click(card, offset=(x, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize(('answer', 'recap', 'summary', 'expected'), [
    ('', 'Completed the requested work. ' * 5, None, 'Completed the requested work. ' * 5),
    ('', None, 'Summary only', 'Summary only'),
    ('Full answer', 'Recap', 'Summary', 'Full answer'),
])
async def test_folded_header_speaks_and_stops_without_unfolding(
    folded_speech_host, answer, recap, summary, expected,
):
    Host, active, spoken = folded_speech_host
    card = AssistantMessage()
    card.recap = recap
    card.set_summary(summary if summary is not None else recap)
    card.set_model_name('model')
    card.set_collapsed(True)
    async with Host(card).run_test(size=(55, 20)) as pilot:
        card.set_answer(answer)
        await pilot.pause(.3)
        row = _header_row(card)
        assert '♫ Speak' in row  # survives a long/truncated summary
        assert row.index('Speak') < row.index(card.summary[:7])
        button = card.query_one(ResponseSpeakButton)
        assert not card.body.display
        assert not button.display  # no duplicate body control in folded layout
        assert not spoken
        await _click_header(pilot, card, 'Speak')
        assert spoken == [(expected, button)]
        assert active == {button}
        assert card.collapsed
        assert '■ Stop' in _header_row(card)
        await _click_header(pilot, card, 'Stop')
        assert not active
        assert card.collapsed
        assert len(spoken) == 1
        # The rest of the header still unfolds rather than speaking.
        await _click_header(pilot, card, card.MARK_SHUT)
        assert not card.collapsed
        assert button.display
        assert 'Speak' not in _header_row(card)
        assert await pilot.click(button)
        assert active == {button}
        # Fold while playing: the header controls the same playback owner.
        await _click_header(pilot, card, card.MARK_OPEN)
        assert card.collapsed
        assert '■ Stop' in _header_row(card)
    assert not active  # removing the card stops its speech


@pytest.mark.asyncio
async def test_folded_header_tracks_tts_blank_and_external_playback(folded_speech_host):
    Host, active, spoken = folded_speech_host
    card = AssistantMessage()
    card.set_collapsed(True)
    app = Host(card)
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause(.3)
        assert 'Speak' not in _header_row(card)
        card.set_summary('Generated summary')
        await pilot.pause(.3)
        assert 'Speak' in _header_row(card)
        app.settings.tts_enabled = False
        await pilot.pause(.3)
        assert 'Speak' not in _header_row(card)
        app.settings.tts_enabled = True
        await pilot.pause(.3)
        assert 'Speak' in _header_row(card)
        assert not spoken
        await _click_header(pilot, card, 'Speak')
        assert 'Stop' in _header_row(card)
        active.clear()  # completion outside either control
        await pilot.pause(.3)
        assert 'Speak' in _header_row(card)
        card.set_summary(None)
        await pilot.pause(.3)
        assert 'Speak' not in _header_row(card)
        assert not card.query_one(ResponseSpeakButton).display
        assert len(spoken) == 1


@pytest.mark.asyncio
async def test_folded_headers_keep_distinct_playback_owners(folded_speech_host):
    Host, active, spoken = folded_speech_host
    cards = [AssistantMessage(), AssistantMessage()]
    for index, card in enumerate(cards):
        card.set_summary(f'Summary {index}')
        card.set_collapsed(True)
    async with Host(*cards).run_test(size=(80, 20)) as pilot:
        await pilot.pause(.3)
        await _click_header(pilot, cards[0], 'Speak')
        first = cards[0].query_one(ResponseSpeakButton)
        assert active == {first}
        await _click_header(pilot, cards[1], 'Speak')
        second = cards[1].query_one(ResponseSpeakButton)
        assert active == {second}
        assert spoken == [('Summary 0', first), ('Summary 1', second)]
        await pilot.pause(.3)
        assert 'Speak' in _header_row(cards[0])
        assert 'Stop' in _header_row(cards[1])


@pytest.mark.asyncio
async def test_idle_folded_header_does_not_repaint(folded_speech_host):
    from textual.drivers.headless_driver import HeadlessDriver

    Host, _, spoken = folded_speech_host
    writes = []

    class CaptureDriver(HeadlessDriver):
        @property
        def is_headless(self):
            return False

        def write(self, data):
            writes.append(data)

    card = AssistantMessage()
    card.set_summary('Ready')
    card.set_collapsed(True)
    async with Host(card, driver_class=CaptureDriver).run_test(headless=False) as pilot:
        await pilot.pause(.3)
        assert 'Speak' in _header_row(card)
        writes.clear()
        await pilot.pause(.6)
        assert not writes
        assert not spoken
