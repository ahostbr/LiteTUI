import io
from litetui import sanitize


def test_terminal_reset_never_writes_escape_sequences_to_pipe(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(sanitize.sys, '__stdout__', output)
    sanitize.reset_terminal_modes()
    assert output.getvalue() == ''


def test_terminal_reset_still_restores_actual_tty(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    output = Terminal()
    monkeypatch.setattr(sanitize.sys, '__stdout__', output)
    sanitize.reset_terminal_modes()
    assert output.getvalue() == sanitize._MOUSE_OFF + sanitize._MOUSE_ON
