"""Pure Textual-to-native palette and canonical custom-token projection."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from textual.color import Color, ColorParseError
from textual.theme import Theme

from litetui import themes

PALETTE_KEYS = (
    "background", "surface", "primary", "panel", "tool_background", "foreground",
    "title", "assistant_border", "thinking_text", "thinking_border", "tool_text",
    "error", "footer_foreground", "footer_background",
)
MAX_NAME = 256
MAX_TOKEN = 256
MAX_DEFINITION = 65536


def encoded(value, limit=MAX_DEFINITION):
    data = json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(data) > limit:
        raise ValueError("Theme definition exceeds its byte budget")
    return data


def definition_digest(theme: Theme) -> str:
    return hashlib.sha256(encoded(asdict(theme))).hexdigest()


def _rgb(value):
    try:
        color = Color.parse(value)
    except ColorParseError as error:
        raise ValueError("Native theme color is not a supported opaque RGB value") from error
    if color.a != 1.0 or color.ansi is not None or color.auto:
        raise ValueError("Native theme colors require opaque RGB; alpha, ANSI and auto colors are unsupported")
    return color.rgb


def resolved_colors(theme: Theme) -> dict[str, tuple[int, int, int]]:
    """Exact semantic mapping shared with the public offline fixture exporter."""
    variables = theme.to_color_system().generate()
    extras = themes.extra_defaults(primary=variables["primary"], bone=variables["foreground"])
    extras.update(theme.variables)
    values = {
        "background": variables["background"],
        "surface": variables["surface"],
        "primary": variables["primary"],
        "panel": variables["panel"],
        "tool_background": variables["surface-darken-2"],
        "foreground": variables["foreground"],
        "title": variables["success"],
        "assistant_border": variables["success-darken-2"],
        "thinking_text": extras["thinking-text"],
        "thinking_border": extras["thinking-box"],
        "tool_text": extras["tool-text"],
        "error": variables["error"],
        "footer_foreground": extras.get("footer-foreground", variables["footer-foreground"]),
        "footer_background": extras.get("footer-background", variables["footer-background"]),
    }
    result = {name: _rgb(value) for name, value in values.items()}
    if any(type(channel) is not int or not 0 <= channel <= 255
           for rgb in result.values() for channel in rgb):
        raise ValueError("Theme contains an unsupported native RGB value")
    return result


def _hex(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) == 7 and value.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in value[1:]):
        return value
    return None


def _resolved(value):
    return "#{:02x}{:02x}{:02x}".format(*_rgb(value))


def token_rows(theme: Theme, raw_tokens=None):
    """Keep stored optional input distinct from derived/omitted effective tokens."""
    variables = theme.to_color_system().generate()
    fallback = themes.extra_defaults(primary=variables["primary"], bone=variables["foreground"])
    rows = []
    for key in themes.THEME_FORM_TOKENS:
        required = key in themes.THEME_TOKENS
        raw = (raw_tokens.get(key) if raw_tokens is not None else
               getattr(theme, key) if required else theme.variables.get(key))
        if raw is not None and (not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_TOKEN):
            raise ValueError("Theme token contains invalid or oversized text")
        if required:
            resolved = _resolved(variables[key])
            source = "explicit" if raw is not None else "derived"
        elif key in themes.THEME_EXTRA_TOKENS:
            actual = (_hex(raw) if raw_tokens is not None else raw) or fallback[key]
            resolved = _resolved(actual)
            source = "explicit" if (_hex(raw) if raw_tokens is not None else raw) else "derived"
        else:
            actual = _hex(raw) if raw_tokens is not None else raw
            resolved = _resolved(actual) if actual else None
            source = "explicit" if resolved is not None else "omitted"
        rows.append({"key": key, "required": required, "raw": raw,
                     "resolved": resolved, "source": source})
    return rows


def custom(name, tokens):
    """Validate the canonical form without registering, selecting or persisting it."""
    if not isinstance(name, str) or len(name.encode("utf-8")) > MAX_NAME:
        raise ValueError("Theme name must be bounded text")
    if not isinstance(tokens, dict) or set(tokens) - set(themes.THEME_FORM_TOKENS):
        raise ValueError("Theme tokens must use the canonical editor fields")
    clean = {}
    for key, value in tokens.items():
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TOKEN:
            raise ValueError(f"Theme token {key} must be bounded text")
        clean[key] = value.strip()
    candidate = themes.theme_from_tokens(name, clean)
    # Textual registers literal names but get_theme interprets commas as fallbacks.
    if candidate.name != candidate.name.strip() or "," in candidate.name:
        raise ValueError("Theme name cannot resolve to one exact registered definition")
    definition_digest(candidate)
    resolved_colors(candidate)
    return candidate, clean
