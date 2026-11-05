"""Disposable Windows clipboard-format fixtures and allocation instrumentation."""
import ctypes
import io
import struct

from PIL import Image


class PrivateClipboard:
    def __init__(self, entries, revision=91):
        self.entries = entries
        self.revision = revision
        self.events = []
        self.buffer = None

    def register(self, name):
        assert name == "PNG"
        return 999

    def opened(self, owner):
        self.events.append("open")
        return True

    def closed(self):
        self.events.append("close")

    def available(self, number):
        return number in self.entries

    def data(self, number):
        self.events.append(("get", number))
        return number

    def size(self, handle):
        return len(self.entries[handle])

    def lock(self, handle):
        self.events.append("lock")
        self.buffer = ctypes.create_string_buffer(self.entries[handle])
        return ctypes.addressof(self.buffer)

    def unlock(self, handle):
        self.events.append("unlock")

    def sequence(self):
        return self.revision


def bitmap(fmt, color):
    stream = io.BytesIO()
    Image.new("RGB", (23, 15), color).save(stream, format="DIB" if fmt == "DIBV5" else fmt)
    data = stream.getvalue()
    if fmt == "DIBV5":
        assert int.from_bytes(data[:4], "little") == 40
        data = struct.pack("<I", 124) + data[4:40] + bytes(84) + data[40:]
    return data


def file_list(paths):
    return struct.pack("<IiiII", 20, 0, 0, 0, 1) + ("\0".join(paths) + "\0\0").encode("utf-16le")
