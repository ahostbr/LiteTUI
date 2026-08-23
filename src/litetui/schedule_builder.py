"""Human patterns <-> cron strings, both directions, honestly.

The job editor's QoL layer: a pattern dropdown and number tickers that
GENERATE the cron string. The string stays the only stored truth -- the
store, the tick loop and /cron add never see this module -- so the builder
is presentation, not schema.

The hard requirement is the way BACK: opening an existing job must populate
the widgets from its string, and a builder that mis-reads "0 17 * * 5" as
daily would silently rewrite the job on the next save. So `recognize` is the
exact inverse of `build` over everything build can emit -- a property the
tests sweep -- and everything else is CUSTOM, shown raw. Recognition also
accepts a few common spellings build never emits (aliases like @daily,
weekday 7 for Sunday): one-way trips, normalised only if a widget is
actually touched.
"""

from __future__ import annotations

import re

from litetui.scheduler import _ALIASES

#: Preset ids and their dropdown labels, in menu order.
PRESETS = [
    ("daily",         "Every day at…"),
    ("weekdays",      "Weekdays (Mon–Fri) at…"),
    ("weekends",      "Weekends (Sat+Sun) at…"),
    ("weekly",        "Every week on…"),
    ("monthly",       "Every month on day…"),
    ("yearly",        "Every year on…"),
    ("every_minutes", "Every N minutes"),
    ("every_hours",   "Every N hours"),
    ("custom",        "Custom (raw cron)"),
]

WEEKDAYS = [
    ("Monday", 1), ("Tuesday", 2), ("Wednesday", 3), ("Thursday", 4),
    ("Friday", 5), ("Saturday", 6), ("Sunday", 0),
]

MONTHS = [
    ("January", 1), ("February", 2), ("March", 3), ("April", 4),
    ("May", 5), ("June", 6), ("July", 7), ("August", 8),
    ("September", 9), ("October", 10), ("November", 11), ("December", 12),
]

#: Defaults for widgets that a recognition did not populate.
DEFAULTS = {"hour": 9, "minute": 0, "weekday": 1, "day": 1, "month": 1, "n": 5}


def build(preset: str, p: dict) -> str:
    """The canonical cron string for a preset + params. Raises on custom --
    custom IS the raw field; there is nothing to build."""
    m, h = int(p.get("minute", 0)), int(p.get("hour", 0))
    if preset == "daily":
        return f"{m} {h} * * *"
    if preset == "weekdays":
        return f"{m} {h} * * 1-5"
    if preset == "weekends":
        return f"{m} {h} * * 0,6"
    if preset == "weekly":
        return f"{m} {h} * * {int(p['weekday'])}"
    if preset == "monthly":
        return f"{m} {h} {int(p['day'])} * *"
    if preset == "yearly":
        return f"{m} {h} {int(p['day'])} {int(p['month'])} *"
    if preset == "every_minutes":
        n = int(p["n"])
        return "* * * * *" if n == 1 else f"*/{n} * * * *"
    if preset == "every_hours":
        n = int(p["n"])
        return "0 * * * *" if n == 1 else f"0 */{n} * * *"
    raise ValueError(f"nothing to build for preset {preset!r}")


_INT = re.compile(r"^\d{1,2}$")
_STEP = re.compile(r"^\*/(\d{1,2})$")


def _as_int(field: str, lo: int, hi: int):
    """The field as an int within bounds, else None. '09' counts as 9."""
    if not _INT.match(field):
        return None
    value = int(field)
    return value if lo <= value <= hi else None


def recognize(schedule: str):
    """(preset, params) for a string the builder understands, else None.

    None is the honest answer, not a failure: it routes the editor to CUSTOM
    with the raw string shown. A recognizer that guessed would let the next
    save silently rewrite a schedule it never understood.
    """
    raw = (schedule or "").strip()
    expanded = _ALIASES.get(raw.lower(), raw)
    fields = expanded.split()
    if len(fields) != 5:
        return None
    m_f, h_f, dom_f, mon_f, dow_f = fields

    # Interval shapes first: their minute/hour fields are not plain ints,
    # so they cannot shadow the time-of-day shapes below.
    if (m_f, h_f, dom_f, mon_f, dow_f) == ("*", "*", "*", "*", "*"):
        return "every_minutes", {"n": 1}
    step = _STEP.match(m_f)
    if step and (h_f, dom_f, mon_f, dow_f) == ("*", "*", "*", "*"):
        n = int(step.group(1))
        return ("every_minutes", {"n": n}) if 2 <= n <= 59 else None
    if m_f == "0" and (dom_f, mon_f, dow_f) == ("*", "*", "*"):
        if h_f == "*":
            return "every_hours", {"n": 1}
        step = _STEP.match(h_f)
        if step:
            n = int(step.group(1))
            return ("every_hours", {"n": n}) if 2 <= n <= 23 else None

    minute = _as_int(m_f, 0, 59)
    hour = _as_int(h_f, 0, 23)
    if minute is None or hour is None:
        return None
    at = {"hour": hour, "minute": minute}

    if (dom_f, mon_f) == ("*", "*"):
        if dow_f == "*":
            return "daily", at
        if dow_f == "1-5":
            return "weekdays", at
        if dow_f == "0,6":
            return "weekends", at
        dow = _as_int(dow_f, 0, 7)
        if dow is not None:
            # 7 is Sunday by convention; build only ever emits 0. A one-way
            # trip, normalised only if the person touches a widget.
            return "weekly", {**at, "weekday": 0 if dow == 7 else dow}
        return None

    dom = _as_int(dom_f, 1, 31)
    if dom is None or dow_f != "*":
        return None            # dom+dow combined is cron's OR trap: custom
    if mon_f == "*":
        return "monthly", {**at, "day": dom}
    mon = _as_int(mon_f, 1, 12)
    if mon is not None:
        return "yearly", {**at, "day": dom, "month": mon}
    return None
