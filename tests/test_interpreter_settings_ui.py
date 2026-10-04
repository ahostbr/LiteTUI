"""Offline mounted settings control contract for explicit interpreter identities."""
from dataclasses import replace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import TextArea

from litetui.settings import Settings
from litetui.settings_screen import SettingsBody, SettingsScreen
from litetui import trusted_executables


@pytest.mark.asyncio
async def test_interpreter_path_lines_roundtrip_losslessly(tmp_path):
    paths = [r'C:\Program Files\Python (fixture)\python.exe',
             r'E:\repo, with spaces\.venv\Scripts\python.exe']
    class Host(App):
        def compose(self) -> ComposeResult:
            return []
        def on_mount(self):
            self.push_screen(SettingsScreen(Settings(tool_trusted_interpreters=paths)))
    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        control = screen.query_one('#f-tool_trusted_interpreters', TextArea)
        assert control.text == '\n'.join(paths)
        assert screen.query_one(SettingsBody)._collect().tool_trusted_interpreters == paths
        body = screen.query_one(SettingsBody)
        state = body.get_state()
        control.load_text('')
        assert body._collect().tool_trusted_interpreters == []
        body.set_state(state)
        assert body._collect().tool_trusted_interpreters == paths
        body._set_dialog_values(Settings(), ('tool_trusted_interpreters',))
        assert body._collect().tool_trusted_interpreters == []
        exe = tmp_path / 'python.exe'
        exe.write_bytes(b'fixture, never executed')
        control.load_text(str(exe) + '\nrelative/python.exe')
        malformed = screen.query_one(SettingsBody)._collect().tool_trusted_interpreters
        assert malformed == [str(exe), 'relative/python.exe']
        assert not trusted_executables.is_configured_interpreter(str(exe), malformed)
        assert replace(screen.query_one(SettingsBody)._collect()).tool_trusted_interpreters == malformed
