"""Read one Windows clipboard representation while holding its ownership lock."""
from __future__ import annotations

import ctypes
import os
import struct
from pathlib import Path
from types import SimpleNamespace

from litetui import native_image

MAX_LIST = 64 * 1024
MAX_FILES = 32
MAX_DIB = native_image.MAX_PIXELS * 4 + 64 * 1024


def _function(dll, name, args, result):
    function = getattr(dll, name)
    function.argtypes = args
    function.restype = result
    return function


def _win32():
    if os.name != "nt":
        raise ValueError("System image paste currently requires Windows. Enter a local image path instead.")
    from ctypes import wintypes as win

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    register = _function(user, "RegisterClipboardFormatW", [win.LPCWSTR], win.UINT)
    opened = _function(user, "OpenClipboard", [win.HWND], win.BOOL)
    closed = _function(user, "CloseClipboard", [], win.BOOL)
    available = _function(user, "IsClipboardFormatAvailable", [win.UINT], win.BOOL)
    data = _function(user, "GetClipboardData", [win.UINT], win.HANDLE)
    sequence = _function(user, "GetClipboardSequenceNumber", [], win.DWORD)
    size = _function(kernel, "GlobalSize", [win.HANDLE], ctypes.c_size_t)
    lock = _function(kernel, "GlobalLock", [win.HANDLE], ctypes.c_void_p)
    unlock = _function(kernel, "GlobalUnlock", [win.HANDLE], win.BOOL)
    return SimpleNamespace(register=register, opened=opened, closed=closed, available=available, data=data,
                           sequence=sequence, size=size, lock=lock, unlock=unlock)


def capture(api=None):
    api = api if api is not None else _win32()
    formats = ((api.register("PNG"), "PNG", native_image.MAX_FILE), (17, "DIBV5", MAX_DIB),
               (8, "DIB", MAX_DIB), (15, "FILES", MAX_LIST))
    if not api.opened(None):
        raise ValueError("Windows clipboard is busy. Try Ctrl+O again.")
    try:
        for number, kind, limit in formats:
            if not number or not api.available(number):
                continue
            handle = api.data(number)
            count = api.size(handle) if handle else 0
            if not 0 < count <= limit:
                raise ValueError("Clipboard image format is unavailable or exceeds its bounded allocation.")
            ptr = api.lock(handle)
            if not ptr:
                raise ValueError("Windows could not read the clipboard image. Try again.")
            try:
                content = ctypes.string_at(ptr, count)
            finally:
                api.unlock(handle)
            revision = api.sequence()
            if not revision:
                raise ValueError("Windows did not identify the clipboard version.")
            return kind, revision, content
    finally:
        api.closed()
    raise ValueError("No supported image in the clipboard. Enter a local image path instead.")


def file_names(data):
    if not 20 <= len(data) <= MAX_LIST:
        raise ValueError("Clipboard file list is invalid or too large.")
    offset, _, _, _, wide = struct.unpack_from("<IiiII", data)
    if not 20 <= offset < len(data):
        raise ValueError("Clipboard file list is malformed.")
    try:
        text = data[offset:].decode("utf-16le" if wide else "mbcs")
    except UnicodeError as exc:
        raise ValueError("Clipboard file list is malformed.") from exc
    end = text.find("\0\0")
    if end < 0:
        raise ValueError("Clipboard file list is incomplete.")
    files = text[:end].split("\0")
    if len(files) > MAX_FILES:
        raise ValueError("Clipboard holds too many files (maximum 32).")
    return files


def read(app):
    kind, revision, data = capture()
    if kind == "FILES":
        for path in file_names(data):
            if Path(path).suffix.lower() in (".png", ".jpeg", ".jpg", ".webp", ".bmp", ".gif"):
                try:
                    details, encoded = native_image.read_image(app, path, formats=["PNG", "JPEG", "WEBP", "GIF", "BMP"])
                    break
                except (ValueError, OSError):
                    continue
        else:
            raise ValueError("Clipboard file list has no readable still image.")
    elif kind in ("PNG", "DIB", "DIBV5"):
        details, encoded = native_image.decode_image("Clipboard image", data, ["PNG" if kind == "PNG" else "DIB"])
    else:
        raise ValueError("Unsupported clipboard image format.")
    return {**details, "clipboard_format": kind, "clipboard_sequence": revision}, encoded
