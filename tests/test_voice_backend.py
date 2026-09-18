"""voice_backend — the TTS-out core. Pure parts asserted directly; speak()'s
launch is checked with a stubbed Popen so the suite never makes a sound."""
from litetui import voice_backend as v


def test_clean_strips_code_paths_and_caps():
    assert "code" not in v.clean_for_speech("read ```code``` and `inline` ok")
    assert v.clean_for_speech("a" * 999) == "a" * 600
    assert v.clean_for_speech("   \n  ") == ""


def test_pyttsx3_is_an_available_engine():
    # It is a declared core dependency, so it must always be present.
    assert "pyttsx3" in v.available_engines()


def test_speak_launches_a_detached_child_for_pyttsx3(monkeypatch):
    calls = {}
    def fake_popen(argv, **kw):
        calls["argv"] = argv; calls["kw"] = kw
        return object()
    monkeypatch.setattr(v.subprocess, "Popen", fake_popen)
    assert v.speak("hello there", engine="pyttsx3") is True
    # a child process, not an in-process call (the whole point — no MCI/thread)
    assert calls["argv"][:2][1] == "-c"
    assert "pyttsx3" in calls["argv"][2]


def test_speak_is_false_for_empty_text(monkeypatch):
    monkeypatch.setattr(v.subprocess, "Popen", lambda *a, **k: object())
    assert v.speak("   ", engine="pyttsx3") is False


def test_speak_is_false_when_engine_unavailable(monkeypatch):
    # edge needs edge_tts+playsound; force them absent -> no child, no crash.
    monkeypatch.setattr(v, "_has", lambda m: False)
    monkeypatch.setattr(v.subprocess, "Popen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("launched!")))
    assert v.speak("hi", engine="edge") is False


def test_a_quote_in_the_text_cannot_break_the_child(monkeypatch):
    grabbed = {}
    monkeypatch.setattr(v.subprocess, "Popen",
                        lambda argv, **k: grabbed.setdefault("src", argv[2]) or object())
    v.speak('he said "hi" and \n newline', engine="pyttsx3")
    # repr() escaping means the payload is a valid python literal, not a break-out
    compile(grabbed["src"], "<child>", "exec")
