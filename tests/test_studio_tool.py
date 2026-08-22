"""The studio tool: offered, dispatched, honest when the apps are closed.

No test here requires LiteImage/LiteSound to be running — the offline arm IS
a first-class behaviour (an end user's app is closed more often than open),
and the happy paths run against a stub HTTP transport. The one thing never
tested with a mock is the error prose, because the error prose is the
product: it is what the model reads and acts on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import studio_tool


# --------------------------------------------------------------------------
# wiring — written is not offered, offered is not dispatched
# --------------------------------------------------------------------------

def test_the_studio_tool_is_offered_to_the_model():
    a = m.LiteTUI()
    a._connect = lambda: None
    names = [t["function"]["name"] for t in a._all_tools() if "function" in t]
    assert "studio" in names, "the spec exists but never reaches the model"


def test_the_dispatcher_resolves_studio_and_injects_the_seat():
    """The dispatch is a closure, not the bare function: it must carry the
    CALLING model's identity in, so generate actions can suspend the very
    seat that ordered them. Identity-checking the function would miss a
    closure that forgot the injection — call it and look."""
    a = m.LiteTUI()
    a._connect = lambda: None
    a.model_id = "the-current-seat"
    seen = {}

    import studio_tool as st
    real = st.run
    try:
        st.run = lambda args, seat_model=None, backend=None: seen.update(
            args=args, seat_model=seat_model, backend=backend) or "ok"
        fn = a._dispatch_for("studio")
        assert fn is not None, "offered but not dispatched"
        out = fn({"app": "image", "action": "status"})
    finally:
        st.run = real

    assert out == "ok"
    assert seen["seat_model"] == "the-current-seat", (
        "the dispatch must inject the CALLER's model id — without it, "
        "generation runs beside a 29 GB resident and the box OOMs again"
    )
    assert seen["backend"] is a.backend, (
        "the dispatch must inject the app's backend — suspend verbs sent to "
        "the wrong engine manage nothing (Sentinel's finding, 2026-08-21)"
    )


def test_the_spec_teaches_the_async_sound_flow():
    """The spec description IS the documentation the model reads. If the
    job_id/poll contract leaves it, the model will fire-and-forget sound
    generations and report them as done."""
    desc = studio_tool.STUDIO_TOOL_SPEC["function"]["description"]
    assert "job_id" in desc
    assert "ASYNC" in desc or "async" in desc
    assert "RUNNING" in desc, "the spec must say the apps have to be open"


# --------------------------------------------------------------------------
# argument validation — refused before any network is touched
# --------------------------------------------------------------------------

def test_unknown_app_is_refused():
    out = studio_tool.run({"app": "banana", "action": "generate"})
    assert "unknown app" in out


def test_image_generate_without_prompt_is_refused():
    out = studio_tool.run({"app": "image", "action": "generate"})
    assert "'prompt' is required" in out


def test_sound_generate_without_mode_is_refused():
    out = studio_tool.run({"app": "sound", "action": "generate", "prompt": "x"})
    assert "mode" in out and "music" in out


def test_sound_generate_without_content_is_refused():
    out = studio_tool.run({"app": "sound", "action": "generate", "mode": "music"})
    assert "'prompt' and/or 'lyrics'" in out


def test_sound_job_without_id_is_refused():
    out = studio_tool.run({"app": "sound", "action": "job"})
    assert "'job_id' is required" in out


# --------------------------------------------------------------------------
# the apps are closed — the common end-user state, answered in prose
# --------------------------------------------------------------------------

@pytest.fixture()
def _dead_ports(monkeypatch):
    """Point both apps at a port nothing listens on. Connection refused is
    immediate on loopback, so these run fast and deterministic."""
    monkeypatch.setattr(studio_tool, "IMAGE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(studio_tool, "SOUND_URL", "http://127.0.0.1:1")


def test_image_down_is_a_sentence_not_a_traceback(_dead_ports):
    out = studio_tool.run({"app": "image", "action": "status"})
    assert "LiteImage not available" in out
    assert "RUNNING" in out
    assert "Traceback" not in out


def test_sound_down_is_a_sentence_not_a_traceback(_dead_ports):
    out = studio_tool.run({"app": "sound", "action": "generate",
                           "mode": "music", "prompt": "rain"})
    assert "LiteSound not available" in out
    assert "Traceback" not in out


def test_model_without_lst_names_the_dependency(monkeypatch):
    monkeypatch.setattr(studio_tool.shutil, "which", lambda _n: None)
    out = studio_tool.run({"app": "model", "action": "status"})
    assert "lst CLI not found" in out


# --------------------------------------------------------------------------
# happy paths against a stub transport — protocol shape, not the GPU
# --------------------------------------------------------------------------

def test_sound_generate_posts_the_mode_path_and_flags_async(monkeypatch):
    calls = {}

    def fake_http(method, url, body=None, timeout=0):
        calls["method"], calls["url"], calls["body"] = method, url, body
        return {"job_id": "j-123", "state": "queued"}

    monkeypatch.setattr(studio_tool, "_http", fake_http)
    out = studio_tool.run({"app": "sound", "action": "generate",
                           "mode": "song", "lyrics": "la la", "duration": 30})

    assert calls["method"] == "POST"
    assert calls["url"].endswith("/generate/song"), calls["url"]
    assert calls["body"] == {"lyrics": "la la", "duration": 30}
    assert "j-123" in out
    assert "poll" in out, "an async result must carry the poll instruction"


def test_image_generate_posts_prompt_and_optionals(monkeypatch):
    calls = {}

    def fake_http(method, url, body=None, timeout=0):
        calls["url"], calls["body"], calls["timeout"] = url, body, timeout
        return {"imagePath": "C:/out/x.png"}

    monkeypatch.setattr(studio_tool, "_http", fake_http)
    out = studio_tool.run({"app": "image", "action": "generate",
                           "prompt": "a fox", "width": 1024, "steps": 20})

    assert calls["url"].endswith("/generate")
    assert calls["body"] == {"prompt": "a fox", "width": 1024, "steps": 20}
    assert calls["timeout"] == studio_tool.IMAGE_GEN_TIMEOUT, (
        "image generation is synchronous — a quick timeout would kill real runs"
    )
    assert "x.png" in out


def test_sound_job_polls_the_job_path(monkeypatch):
    seen = {}

    def fake_http(method, url, body=None, timeout=0):
        seen["method"], seen["url"] = method, url
        return {"state": "done", "file": "C:/out/song.mp3"}

    monkeypatch.setattr(studio_tool, "_http", fake_http)
    out = studio_tool.run({"app": "sound", "action": "job", "job_id": "j-9"})
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/job/j-9")
    assert "song.mp3" in out


def test_large_results_are_truncated_not_dumped():
    big = {"rows": ["x" * 100] * 200}
    out = studio_tool._fmt(big)
    assert len(out) < 4200
    assert "truncated" in out


def test_env_overrides_are_read_at_import(monkeypatch):
    """The URLs come from env so an end user (or a test) can repoint them.
    This pins that they are module constants derived from env, not literals
    buried in call sites."""
    src = (Path(__file__).resolve().parent.parent / "src" / "studio_tool.py").read_text(
        encoding="utf-8")
    assert 'os.environ.get("LITEIMAGE_API_URL"' in src
    assert 'os.environ.get("LITESOUND_API_URL"' in src
