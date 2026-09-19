"""`chrome action=shot` attaches its PNG to the next message automatically, so a
vision model sees the screenshot without a second view_image call.

Ryan, live: "action shot to return the image automatically instead of a separate
view image call."

Routes through LiteTUI._maybe_stage_shot with a fake self — it only touches
self.model_type and self._stage_image_path, so no running app is needed.
"""
from __future__ import annotations

import types

from litetui.app import LiteTUI


def _maybe(model_type, name, args, result, stager=None):
    calls = []

    def _stage(p):
        calls.append(p)
        return None

    fake = types.SimpleNamespace(model_type=model_type,
                                 _stage_image_path=(stager or _stage))
    out = LiteTUI._maybe_stage_shot(fake, name, args, result)
    return out, calls


def test_vlm_shot_attaches_and_replaces_the_path():
    out, calls = _maybe("vlm", "chrome", {"action": "shot"}, "Saved C:/x/chrome-shot.png.")
    assert "attached" in out.lower()
    assert len(calls) == 1


def test_text_only_model_keeps_the_path_and_stages_nothing():
    out, calls = _maybe("llm", "chrome", {"action": "shot"}, "Saved C:/x/chrome-shot.png.")
    assert out == "Saved C:/x/chrome-shot.png."
    assert calls == []


def test_a_non_shot_chrome_action_is_untouched():
    out, calls = _maybe("vlm", "chrome", {"action": "text"}, "page text here")
    assert out == "page text here"
    assert calls == []


def test_an_errored_shot_is_not_attached():
    out, calls = _maybe("vlm", "chrome", {"action": "shot"}, "[error] chrome shot: nothing")
    assert out.startswith("[error]")
    assert calls == []


def test_a_non_chrome_tool_is_untouched():
    out, calls = _maybe("vlm", "view_image", {"action": "shot"}, "whatever")
    assert out == "whatever"
    assert calls == []


def test_a_staging_failure_appends_the_error_and_keeps_the_path():
    out, calls = _maybe("vlm", "chrome", {"action": "shot"}, "Saved C:/x/chrome-shot.png.",
                        stager=lambda p: "[error] no such file: x")
    assert "Saved C:/x/chrome-shot.png." in out
    assert "attach" in out.lower()


def test_an_identical_shot_is_not_staged_again(monkeypatch):
    """Measured 2026-09-19: a stuck model re-shot the same viewport 16+ times
    and every identical PNG was re-attached, costing tokens while saying
    nothing. The tool result already explains why the picture is unchanged,
    so an unchanged picture is not re-sent — the text stands on its own."""
    from litetui import chrome_tool
    monkeypatch.setattr(chrome_tool, "last_shot_identical", True)
    out, calls = _maybe(
        "vlm", "chrome", {"action": "shot"},
        "Identical to the previous screenshot — the page has NOT changed "
        "since your last shot, so shooting again will produce the same "
        "picture. Act on what you already see — or change the page first "
        "(scroll, click, navigate) and shoot again.",
    )
    assert calls == []
    assert "NOT changed" in out
