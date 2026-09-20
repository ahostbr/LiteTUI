import pytest
from textual.app import App
from litetui.input_controls import PromptBox


class Demo(App):
    def compose(self):
        yield PromptBox()
    def action_toggle_scroll_lock(self): self.clicked = 'lock'
    def action_toggle_speak(self): self.clicked = 'speak'
    def action_toggle_mic(self): self.clicked = 'mic'
    def action_toggle_pause(self): self.clicked = 'pause'


@pytest.mark.asyncio
async def test_controls_share_input_border_and_click():
    from litetui.app import LiteTUI
    class StyledDemo(Demo):
        CSS = LiteTUI.CSS
        def get_css_variables(self):
            from litetui import themes as themes_mod
            variables = super().get_css_variables()
            for key, value in themes_mod.extra_defaults(primary=variables['primary'], bone=variables['foreground']).items():
                variables.setdefault(key, value)
            return variables
    app = StyledDemo()
    async with app.run_test(size=(100, 20)) as pilot:
        await pilot.pause(0.1)
        box = app.query_one('#message-input').region
        lock = app.query_one('#scroll-lock').region
        actions = app.query_one('#prompt-actions').region
        assert lock.y == box.y
        assert actions.y == box.bottom - 1
        assert box.right - 3 <= lock.right <= box.right
        assert box.right - 3 <= actions.right <= box.right
        for selector, result in [('#scroll-lock','lock'), ('#speak-toggle','speak'), ('.mic-button','mic'), ('.pause-button','pause')]:
            assert await pilot.click(selector)
            assert app.clicked == result
        app.save_screenshot('prompt-controls.svg', path='C:/Projects/LiteTUI/artifacts')


def test_unlock_rejects_even_reader_acted_scroll():
    from types import SimpleNamespace
    from litetui.app import LiteTUI
    app = SimpleNamespace(settings=SimpleNamespace(autoscroll=False))
    LiteTUI._scroll_down(app, reader_acted=True)


def test_tts_default_and_owned_stop(monkeypatch):
    from litetui import voice_backend as voice
    from litetui.settings import Settings
    assert Settings().tts_timeout == 300
    class Proc:
        killed = False
        def poll(self): return None
        def kill(self): self.killed = True
    proc = Proc()
    monkeypatch.setattr(voice, '_active', {proc})
    voice.stop()
    assert proc.killed
    assert not voice.speak('hello', timeout=0)

@pytest.mark.asyncio
async def test_real_scroll_growth_unlock_and_relock():
    from types import SimpleNamespace
    from textual.widgets import Static
    from litetui.app import LiteTUI, ChatLog
    class ScrollDemo(Demo):
        _scroll_down = LiteTUI._scroll_down
        _next_follow_generation = LiteTUI._next_follow_generation
        action_toggle_scroll_lock = LiteTUI.action_toggle_scroll_lock
        _refresh_prompt_controls = LiteTUI._refresh_prompt_controls
        _follow_generation = 0
        settings = SimpleNamespace(autoscroll=True, tts_enabled=False)
        def _autocollapse_offscreen(self, log): pass
        def compose(self):
            yield ChatLog(id='chat-log')
            yield PromptBox()
    app = ScrollDemo()
    async with app.run_test(size=(100, 25)) as pilot:
        log = app.query_one('#chat-log')
        content = Static('\n'.join(str(i) for i in range(100)))
        await log.mount(content)
        app._scroll_down()
        await pilot.pause(0.1)
        assert log.scroll_y == log.max_scroll_y
        # A queued scroll must not run after unlocking.
        app._scroll_down()
        app.action_toggle_scroll_lock()
        log.scroll_to(y=10, animate=False, immediate=True)
        content.update('\n'.join(str(i) for i in range(150)))
        app._scroll_down(reader_acted=True)
        await pilot.pause(0.1)
        assert log.scroll_y == 10
        app.action_toggle_scroll_lock()
        await pilot.pause(0.1)
        assert log.scroll_y == log.max_scroll_y
        content.update('\n'.join(str(i) for i in range(50)))
        app._scroll_down()
        await pilot.pause(0.1)
        assert log.scroll_y == log.max_scroll_y


def test_tts_timeout_kills_and_reaps_owned_process(monkeypatch):
    import subprocess
    from litetui import voice_backend as voice
    class Proc:
        killed = False
        waits = []
        def wait(self, timeout):
            self.waits.append(timeout)
            if not self.killed: raise subprocess.TimeoutExpired('speech', timeout)
        def kill(self): self.killed = True
    proc = Proc()
    monkeypatch.setattr(voice, '_active', {proc})
    voice._reap(proc, 300)
    assert proc.killed
    assert proc.waits == [300, 5]
    assert proc not in voice._active


def test_long_speech_uses_file_not_windows_command_line(monkeypatch):
    from pathlib import Path
    from litetui import voice_backend as voice
    captured = {}
    class Proc:
        def wait(self, timeout): captured['timeout'] = timeout
    def launch(args, **kwargs):
        captured['args'] = args
        captured['script'] = Path(args[1]).read_text(encoding='utf-8')
        return Proc()
    class Thread:
        def __init__(self, target, args, **kwargs): self.target, self.args = target, args
        def start(self): self.target(*self.args)
    monkeypatch.setattr(voice, '_has', lambda name: True)
    monkeypatch.setattr(voice.subprocess, 'Popen', launch)
    monkeypatch.setattr(voice.threading, 'Thread', Thread)
    assert voice.speak('hello ' * 10000, timeout=450)
    assert len(captured['args']) == 2
    assert 'hello ' * 9999 in captured['script']
    assert captured['timeout'] == 450
    assert not Path(captured['args'][1]).exists()
