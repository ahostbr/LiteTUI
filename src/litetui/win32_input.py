"""Windows console input filtering owned by LiteTUI.

ConPTY converts a text write into console ``INPUT_RECORD`` objects. To express
control and shifted characters it synthesizes modifier key-down records whose
``UnicodeChar`` is NUL before the character record. Textual 8.1 forwards every
key-down character, including those modifier-only NULs, so its XTerm parser
turns them into ``ctrl+@``. This module removes only those structural records;
a real Ctrl+Space record has VK_SPACE and survives.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, TypeVar, cast

_Record = TypeVar("_Record")
_LOG = logging.getLogger(__name__)

# Shift, Control, Alt, Caps Lock, left/right Windows, and the sided
# Shift/Control/Alt virtual keys.
_MODIFIER_ONLY_VKS = frozenset(
    (*range(0x10, 0x13), 0x14, 0x5B, 0x5C, *range(0xA0, 0xA6))
)


def is_synthetic_modifier_keydown(record: object) -> bool:
    """True only for a modifier key-down carrying no character."""
    if getattr(record, "EventType", None) != 0x0001:
        return False
    key = cast(Any, record).Event.KeyEvent
    return bool(
        key.bKeyDown
        and key.wVirtualKeyCode in _MODIFIER_ONLY_VKS
        and key.uChar.UnicodeChar == "\x00"
    )


def filter_input_records(records: Iterable[_Record]) -> list[_Record]:
    """Drop ConPTY's structural modifier records, preserving real NUL keys."""
    return [record for record in records if not is_synthetic_modifier_keydown(record)]


def install() -> None:
    """Install LiteTUI's filter at Textual's console-read boundary on Windows."""
    try:
        from textual.drivers import win32
    except ImportError:
        return
    if getattr(win32, "_litetui_input_filter_installed", False):
        return

    try:
        kernel32 = cast(Any, win32.KERNEL32)
        original = kernel32.ReadConsoleInputW
    except AttributeError:
        _LOG.warning(
            "Textual's Windows input boundary changed; "
            "LiteTUI's ConPTY modifier filter was not installed"
        )
        return

    def read_console_input(handle, records, length, read_count):
        result = original(handle, records, length, read_count)
        if not result or not read_count._obj.value:
            return result
        count = read_count._obj.value
        kept = filter_input_records(records._obj[:count])
        for index, record in enumerate(kept):
            records._obj[index] = record
        read_count._obj.value = len(kept)
        return result

    kernel32.ReadConsoleInputW = read_console_input
    cast(Any, win32)._litetui_input_filter_installed = True
