"""wants_ansi_fallback — the legacy-conhost color fallback gate.

The defect it guards (sandbox 0057, 2026-09-01): litetui in a plain conhost
rendered its green theme as broken blue 16-color bands around the header and
composer. The gate flips Textual to ``ansi_color=True`` there, which adopts
the console's own palette — and must stay False everywhere modern, or every
Windows Terminal user silently loses truecolor.
"""

from __future__ import annotations

from types import SimpleNamespace

from litetui.app import wants_ansi_fallback


def test_false_off_windows(monkeypatch):
    monkeypatch.setattr("litetui.app.sys.platform", "linux")
    assert wants_ansi_fallback() is False


def test_false_inside_windows_terminal(monkeypatch):
    # WT_SESSION is the cheap discriminator: Windows Terminal always sets it,
    # and the whole point of the gate is to never fire there.
    monkeypatch.setattr("litetui.app.sys.platform", "win32")
    monkeypatch.setenv("WT_SESSION", "some-session-guid")
    assert wants_ansi_fallback() is False


def _fake_features(monkeypatch, vt: bool, truecolor: bool) -> None:
    import rich._windows

    monkeypatch.setattr(
        rich._windows,
        "get_windows_console_features",
        lambda: SimpleNamespace(vt=vt, truecolor=truecolor),
    )


def test_true_on_legacy_conhost(monkeypatch):
    # The measured 0057 state: conhost where VT enabling failed outright.
    monkeypatch.setattr("litetui.app.sys.platform", "win32")
    monkeypatch.delenv("WT_SESSION", raising=False)
    _fake_features(monkeypatch, vt=False, truecolor=False)
    assert wants_ansi_fallback() is True


def test_false_when_console_takes_truecolor(monkeypatch):
    # A ConPTY-style console without WT_SESSION but with full VT: no fallback.
    monkeypatch.setattr("litetui.app.sys.platform", "win32")
    monkeypatch.delenv("WT_SESSION", raising=False)
    _fake_features(monkeypatch, vt=True, truecolor=True)
    assert wants_ansi_fallback() is False


def test_vt_without_truecolor_still_falls_back(monkeypatch):
    # Half-capable consoles get the coherent ANSI palette too — truecolor
    # emitted onto a 16-color VT console is the same quantization defect.
    monkeypatch.setattr("litetui.app.sys.platform", "win32")
    monkeypatch.delenv("WT_SESSION", raising=False)
    _fake_features(monkeypatch, vt=True, truecolor=False)
    assert wants_ansi_fallback() is True


def test_probe_failure_means_no_fallback(monkeypatch):
    # If the feature probe itself blows up, prefer the modern default —
    # a wrongly-engaged fallback downgrades every render it touches.
    import rich._windows

    monkeypatch.setattr("litetui.app.sys.platform", "win32")
    monkeypatch.delenv("WT_SESSION", raising=False)

    def _boom():
        raise OSError("no console handle")

    monkeypatch.setattr(rich._windows, "get_windows_console_features", _boom)
    assert wants_ansi_fallback() is False
