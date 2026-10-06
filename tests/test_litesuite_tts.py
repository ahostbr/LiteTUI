"""No sockets, subprocesses or audio: exercise dispatch/fallback boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import litesuite_tts as ls, voice_backend as voice


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def read(self, limit):
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


@pytest.fixture
def api(monkeypatch):
    state = {"now": 0.0, "calls": [], "health": Response({"ok": True, "app": "LiteSuite"}),
             "speech": Response({"ok": True, "queued": True}), "connect_error": None,
             "request_error": None, "closed": 0, "close_error_at": None}

    class Connection:
        def __init__(self, host, port, timeout):
            assert host == "127.0.0.1"
            self.timeout, self.port = timeout, port

        def connect(self):
            if state["connect_error"]:
                raise state["connect_error"]

        def request(self, method, path, body=None, headers=None):
            self.method = method
            state["calls"].append((method, path, body, headers, self.port, self.timeout))
            if method == "POST" and state["request_error"]:
                raise state["request_error"]

        def getresponse(self):
            result = state["health" if self.method == "GET" else "speech"]
            if isinstance(result, Exception):
                raise result
            return result

        def close(self):
            state["closed"] += 1
            if state["closed"] == state["close_error_at"]:
                raise OSError("socket cleanup failed")

    monkeypatch.delenv("LITESUITE_VOICE_API_PORT", raising=False)
    monkeypatch.setattr(ls, "_health", None)
    # Replace this client's clock, not Python's shared time module (Textual uses it).
    monkeypatch.setattr(ls, "time", SimpleNamespace(monotonic=lambda: state["now"]))
    monkeypatch.setattr(ls.http.client, "HTTPConnection", Connection)
    # Safety guard: a mistaken fallback must never launch a real audio process.
    monkeypatch.setattr(voice.optional_python, "resolve", lambda *modules: None)
    return state


def test_litesuite_first_without_local_dependencies(api):
    assert voice.speak('Hello "there" ```code```') is True
    method, path, body, headers, port, timeout = api["calls"][1]
    assert (method, path, port, timeout) == ("POST", "/v1/tts/speak", 7438, 0.75)
    assert json.loads(body) == {"text": 'Hello "there"', "summarize": False}
    assert headers["X-LiteSuite-Origin"] == "litetui"
    assert not voice.is_playing(object())  # Acknowledgement is not local playback.
    assert api["closed"] == 2


@pytest.mark.parametrize("up", [True, False])
def test_health_cleanup_error_preserves_probe_result(api, up):
    api["close_error_at"] = 1
    if not up:
        api["health"] = TimeoutError()
    assert ls.speak("hello") is (True if up else None)
    assert [c[0] for c in api["calls"]] == (["GET", "POST"] if up else ["GET"])


@pytest.mark.parametrize("accepted", [True, False])
def test_post_cleanup_error_preserves_outcome_without_local_replay(api, monkeypatch, accepted):
    api["close_error_at"] = 2
    api["speech"] = Response({"ok": True, "queued": False}) if accepted else TimeoutError()
    def unexpected(*args):
        pytest.fail("POST cleanup failure must not trigger local fallback")
    monkeypatch.setattr(voice.optional_python, "resolve", unexpected)
    assert voice.speak("hello") is accepted
    assert [c[0] for c in api["calls"]] == ["GET", "POST"]
    assert api["closed"] == 2


def test_cached_up_still_posts_every_utterance_and_expires(api):
    assert ls.speak("one") is True
    assert ls.speak("two") is True
    assert [c[0] for c in api["calls"]] == ["GET", "POST", "POST"]
    api["now"] = 2.1
    assert ls.speak("three") is True
    assert [c[0] for c in api["calls"]] == ["GET", "POST", "POST", "GET", "POST"]


def test_down_cache_expires_and_recovers(api):
    api["health"] = ConnectionRefusedError()
    assert ls.speak("one") is None
    api["health"] = Response({"ok": True, "app": "LiteSuite"})
    assert ls.speak("two") is None
    assert len(api["calls"]) == 1
    api["now"] = 2.1
    assert ls.speak("three") is True
    assert [c[0] for c in api["calls"]] == ["GET", "GET", "POST"]


@pytest.mark.parametrize("health", [TimeoutError(), Response({}, 503), Response(b"broken"), Response({"ok": True, "app": "other"})])
def test_failed_probe_allows_fallback_without_submitting(api, health):
    api["health"] = health
    assert ls.speak("hello") is None
    assert [c[0] for c in api["calls"]] == ["GET"]


def test_definitive_pre_submission_refusal_is_safe(api):
    api["connect_error"] = ConnectionRefusedError()
    assert ls.speak("hello") is None
    assert [c[0] for c in api["calls"]] == ["GET"]
    assert ls.speak("cached down") is None
    assert len(api["calls"]) == 1


@pytest.mark.parametrize("reply", [TimeoutError(), ConnectionResetError(), Response({}, 503),
    Response({"ok": False}), Response({"ok": True, "dropped": True}), Response(b"broken"), Response([])])
def test_ambiguous_or_refused_post_never_replays_locally(api, monkeypatch, reply):
    api["speech"] = reply
    def unexpected(*args):
        pytest.fail("Local engine consulted after possibly accepted POST")
    monkeypatch.setattr(voice.optional_python, "resolve", unexpected)
    assert voice.speak("hello") is False
    assert [c[0] for c in api["calls"]] == ["GET", "POST"]


def test_request_write_failure_never_replays(api):
    api["request_error"] = ConnectionRefusedError()
    assert ls.speak("hello") is False  # Refusal is safe only during connect().


def test_connect_timeout_is_not_claimed_as_definite_refusal(api):
    api["connect_error"] = TimeoutError()
    assert ls.speak("hello") is False


def test_custom_port_and_invalid_configuration(api, monkeypatch):
    monkeypatch.setenv("LITESUITE_VOICE_API_PORT", "8438")
    assert ls.speak("hello") is True
    assert all(c[4] == 8438 for c in api["calls"])
    monkeypatch.setenv("LITESUITE_VOICE_API_PORT", "invalid")
    assert ls.speak("hello") is None


@pytest.mark.parametrize("prefer", [True, False])
def test_chosen_local_engine_and_voice_preserved(api, monkeypatch, prefer):
    api["health"] = ConnectionRefusedError()
    captured = {}
    class Child:
        def wait(self, timeout): pass
    class Thread:
        def __init__(self, target, args, **kwargs): self.target, self.args = target, args
        def start(self): self.target(*self.args)
    def launch(argv, **kwargs):
        captured["source"] = Path(argv[1]).read_text(encoding="utf-8")
        return Child()
    monkeypatch.setattr(voice.optional_python, "resolve", lambda *modules: "python")
    monkeypatch.setattr(voice.subprocess, "Popen", launch)
    monkeypatch.setattr(voice.threading, "Thread", Thread)
    assert voice.speak("hello", engine="edge", voice="chosen-voice", litesuite_first=prefer)
    assert "edge_tts" in captured["source"] and "chosen-voice" in captured["source"]
    assert len(api["calls"]) == (1 if prefer else 0)


def test_empty_or_invalid_budget_never_contacts_api(api):
    assert voice.speak(" ") is False
    assert voice.speak("hello", timeout=0) is False
    assert api["calls"] == []


@pytest.mark.asyncio
async def test_voice_settings_preference_and_explicit_fallback_test(api, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from textual.app import App
    from textual.widgets import Switch
    from litetui.settings import Settings
    from litetui.settings_screen import SettingsBody, SettingsScreen
    from litetui.settings_scope import SETTING_SPECS, SettingScope
    from litetui.settings_ui_model import SETTINGS_SECTIONS

    calls = []
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(voice, "list_sapi_voices", lambda: [])
    monkeypatch.setattr(voice, "speak", lambda text, **kwargs: calls.append(kwargs) or True)

    class Host(App):
        backend = SimpleNamespace(name="lmstudio")
        model_id = "local-model"
        def __init__(self):
            super().__init__()
            self.settings = Settings()
        def on_mount(self):
            self.push_screen(SettingsScreen(self.settings))

    assert Settings().tts_litesuite_first is True
    assert SETTING_SPECS["tts_litesuite_first"].scope == SettingScope.DEVICE
    section = next(s for s in SETTINGS_SECTIONS if s.section_id == "voice-speak")
    assert "tts_litesuite_first" in [f.name for f in section.fields]
    async with Host().run_test(size=(120, 45)) as pilot:
        body = pilot.app.screen.query_one(SettingsBody)
        preference = body.query_one("#f-tts_litesuite_first", Switch)
        assert preference.value is True
        preference.value = False
        await pilot.pause()
        assert body._collect().tts_litesuite_first is False
        body._voice_test()
        assert calls[0]["litesuite_first"] is False
        assert calls[0]["engine"] == "pyttsx3"
    assert api["calls"] == []
