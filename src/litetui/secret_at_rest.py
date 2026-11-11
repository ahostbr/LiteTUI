"""Credentials sealed at rest in the device settings.json.

A sealed value is ``dpapi:v1:<base64>``: the Windows Data Protection API
(CryptProtectData) bound to the CURRENT WINDOWS ACCOUNT, which on Windows 7 and
later encrypts with AES-256 under a key derived from that account's login. No
key file sits beside settings.json, so a copied settings.json is useless on
another account or machine. It does NOT stop code already running as the same
Windows user -- every LiteTUI seat must be able to read the key, and so can any
other program that user runs.

The contract is the boundary only: Settings fields hold PLAINTEXT in memory,
``to_disk`` seals on every write and ``from_disk`` unseals on every read
(``settings._coerce`` is the one read path shared by ``settings.load`` and the
``SettingsService`` snapshots). Off Windows there is no OS keystore here and
values stay plaintext; ``protection()`` says which applies.

A value that cannot be unsealed (another account, a corrupted line) is returned
SEALED, never as blank: blank would silently fall through to $ANTHROPIC_API_KEY
or report "no key", hiding that a saved key exists and is unreadable.
"""
from __future__ import annotations

import base64
import sys

from litetui.sidecar_settings import SECRET_FIELDS

#: Fields sealed at rest: every provider credential (the user, 2026-11: "do all
#: providers"). One list with the sidecar's never-send-the-value set, so a new
#: provider key is masked AND sealed by one edit. A LiteTUI build older than
#: this module reads the sealed text as the key itself: restart every seat.
SEALED_FIELDS = SECRET_FIELDS

PREFIX = "dpapi:v1:"
_UI_FORBIDDEN = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: never prompt


class SecretUnavailable(Exception):
    """A sealed value this Windows account cannot decrypt."""


def is_sealed(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def protection() -> str:
    return "dpapi" if sys.platform == "win32" else "plaintext"


def _entropy(name: str) -> bytes:
    # Binds a blob to its field: a sealed value moved to another field fails.
    return ("litetui.settings." + name).encode()


def _dpapi(protect: bool, data: bytes, entropy: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    src, _keep_src = blob(data)
    ent, _keep_ent = blob(entropy)
    out = Blob()
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def seal(name: str, plain: str) -> str:
    """Seal one field value. Raises OSError rather than writing plaintext on Windows."""
    if not plain or is_sealed(plain) or protection() != "dpapi":
        return plain
    return PREFIX + base64.b64encode(_dpapi(True, plain.encode(), _entropy(name))).decode("ascii")


def unseal(name: str, stored: str) -> str:
    """Plaintext of a stored value; unsealed (legacy/env) values pass through."""
    if not is_sealed(stored):
        return stored
    if protection() != "dpapi":
        raise SecretUnavailable("sealed with Windows DPAPI; not readable on this platform")
    try:
        return _dpapi(False, base64.b64decode(stored[len(PREFIX):], validate=True),
                      _entropy(name)).decode()
    except (OSError, ValueError) as exc:
        raise SecretUnavailable(str(exc)) from None


def to_disk(name: str, value):
    return seal(name, value) if name in SEALED_FIELDS and isinstance(value, str) else value


def from_disk(name: str, raw):
    if name not in SEALED_FIELDS or not is_sealed(raw):
        return raw
    try:
        return unseal(name, raw)
    except SecretUnavailable:
        return raw  # stays sealed: callers report "saved key unreadable", not "no key"


def migrate_file(path) -> None:
    """Seal any plaintext SEALED_FIELDS value already in settings.json. Idempotent."""
    if protection() != "dpapi":
        return
    from litetui.settings_service import _read, _write
    from litetui.shared_state import coordinated_write

    def pending(raw):
        return [k for k in SEALED_FIELDS if isinstance(raw.get(k), str) and raw[k] and not is_sealed(raw[k])]

    raw, _ = _read(path)
    if not pending(raw):
        return
    with coordinated_write(path):
        raw, _ = _read(path)
        names = pending(raw)
        if not names:
            return
        for name in names:
            raw[name] = seal(name, raw[name])
        _write(path, raw)
