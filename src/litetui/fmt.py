"""Shared pure formatters.

The host UI and plugin modules both need these, and neither may import the
other: app.py importing a plugin module re-accretes the monolith (a test
gates it), and a plugin importing app inverts the layering. Common ground
lives here instead.
"""


def fmt_dur(seconds: float) -> str:
    """Human duration, pure. <60s -> 'X.Ys'; >=60s -> 'Mm SS.s'."""
    s = max(0.0, float(seconds))
    if s < 60:
        return f"{s:.1f}s"
    m = int(s // 60)
    return f"{m}m {s - m * 60:04.1f}s"
