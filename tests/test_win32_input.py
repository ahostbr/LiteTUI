"""ConPTY modifier records must not become phantom ctrl+@ key events."""
from __future__ import annotations

import inspect
import logging
from ctypes import byref, wintypes

from textual._xterm_parser import XTermParser
from textual.drivers import win32
from textual.drivers.win32 import INPUT_RECORD

from litetui import win32_input
from litetui.win32_input import filter_input_records

KEY_EVENT = 0x0001
SHIFT_PRESSED = 0x0010
LEFT_CTRL_PRESSED = 0x0008


def _record(vk: int, char: str, *, down: bool, state: int) -> INPUT_RECORD:
    record = INPUT_RECORD()
    record.EventType = KEY_EVENT
    key = record.Event.KeyEvent
    key.bKeyDown = down
    key.wRepeatCount = 1
    key.wVirtualKeyCode = vk
    key.uChar.UnicodeChar = char
    key.dwControlKeyState = state
    return record


def _parse_records(records: list[INPUT_RECORD]) -> list[str]:
    keys = [
        record.Event.KeyEvent.uChar.UnicodeChar
        for record in records
        if record.EventType == KEY_EVENT and record.Event.KeyEvent.bKeyDown
    ]
    parser = XTermParser()
    events = list(parser.feed("".join(keys)))
    events.extend(parser.tick())
    return [event.key for event in events if hasattr(event, "key")]


def _events(records: list[INPUT_RECORD]) -> list[str]:
    return _parse_records(filter_input_records(records))


def _conpty_char(char: str) -> list[INPUT_RECORD]:
    code = ord(char)
    if char == "\x15":
        return [
            _record(0x11, "\x00", down=True, state=LEFT_CTRL_PRESSED),
            _record(0x55, char, down=True, state=LEFT_CTRL_PRESSED),
            _record(0x55, char, down=False, state=LEFT_CTRL_PRESSED),
            _record(0x11, "\x00", down=False, state=0),
        ]
    if char.isupper():
        return [
            _record(0x10, "\x00", down=True, state=SHIFT_PRESSED),
            _record(code, char, down=True, state=SHIFT_PRESSED),
            _record(code, char, down=False, state=SHIFT_PRESSED),
            _record(0x10, "\x00", down=False, state=0),
        ]
    return [
        _record(ord(char.upper()), char, down=True, state=0),
        _record(ord(char.upper()), char, down=False, state=0),
    ]


def test_measured_conpty_modifier_records_do_not_reach_textual_parser() -> None:
    assert _events(_conpty_char("\x15")) == ["ctrl+u"]
    assert _events(_conpty_char("A")) == ["A"]
    assert _events(_conpty_char("x")) == ["x"]


def test_human_style_capitalized_text_has_no_phantom_mic_key() -> None:
    records = [record for char in "Hello World" for record in _conpty_char(char)]
    events = _events(records)
    assert "ctrl+@" not in events
    assert events == list("Hello") + ["space"] + list("World")


def test_real_ctrl_space_is_preserved_exactly_once() -> None:
    # A real Ctrl+Space has VK_SPACE, not a modifier VK. Its NUL is intent.
    records = [
        _record(0x20, "\x00", down=True, state=LEFT_CTRL_PRESSED),
        _record(0x20, "\x00", down=False, state=LEFT_CTRL_PRESSED),
    ]
    assert _events(records) == ["ctrl+@"]


def _reported_payloads() -> dict[str, str]:
    prompt = (
        "Please Review T1005: Ctrl+U, F7, DEL; mixed CASE! "
        "Does punctuation survive? Yes -- exactly. "
    )
    return {
        "ctrl+u": "\x15",
        "559-char prompt without Enter": (prompt * 7)[:559],
        "F7": "\x1b[18~",
        "DEL": "\x1b[3~",
        "x": "x",
        "empty": "",
    }


def test_all_reported_bridge_payloads_are_safe_across_50_writes() -> None:
    for label, payload in _reported_payloads().items():
        starts = 0
        for _ in range(50):
            records = [record for char in payload for record in _conpty_char(char)]
            starts += _events(records).count("ctrl+@")
        assert starts == 0, f"{label}: {starts}/50 writes reached the mic binding"


def test_installed_wrapper_filters_real_ctypes_buffers(monkeypatch) -> None:
    class FakeKernel32:
        def __init__(self) -> None:
            self.pending: list[INPUT_RECORD] = []

        def ReadConsoleInputW(self, _handle, records, length, read_count):
            assert len(self.pending) <= length
            for index, record in enumerate(self.pending):
                records._obj[index] = record
            read_count._obj.value = len(self.pending)
            return 1

    fake = FakeKernel32()
    monkeypatch.setattr(win32, "KERNEL32", fake)
    monkeypatch.setattr(win32, "_litetui_input_filter_installed", False, raising=False)
    win32_input.install()

    def read(records: list[INPUT_RECORD]) -> list[str]:
        fake.pending = records
        capacity = max(1, len(records))
        output = (INPUT_RECORD * capacity)()
        count = wintypes.DWORD(0)
        assert fake.ReadConsoleInputW(None, byref(output), capacity, byref(count))
        return _parse_records(list(output[: count.value]))

    for label, payload in {
        **_reported_payloads(),
        "Hello World": "Hello World",
    }.items():
        starts = 0
        for _ in range(50):
            records = [record for char in payload for record in _conpty_char(char)]
            starts += read(records).count("ctrl+@")
        assert starts == 0, f"{label}: {starts}/50 writes reached the installed filter"

    ctrl_space = [
        _record(0x20, "\x00", down=True, state=LEFT_CTRL_PRESSED),
        _record(0x20, "\x00", down=False, state=LEFT_CTRL_PRESSED),
    ]
    assert read(ctrl_space) == ["ctrl+@"]


def test_textual_event_monitor_still_uses_wrapped_boundary() -> None:
    source = inspect.getsource(win32.EventMonitor.run)
    assert "KERNEL32.ReadConsoleInputW" in source


def test_install_warns_if_textual_input_boundary_changed(monkeypatch, caplog) -> None:
    monkeypatch.setattr(win32, "_litetui_input_filter_installed", False, raising=False)
    monkeypatch.delattr(win32, "KERNEL32")
    with caplog.at_level(logging.WARNING, logger="litetui.win32_input"):
        win32_input.install()
    assert "input boundary changed" in caplog.text
