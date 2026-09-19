"""stt_backend — voice-in core. No real mic or model touched."""
from litetui import stt_backend as s


def test_list_mics_returns_a_list():
    assert isinstance(s.list_mics(), list)


def test_record_stop_none_is_none():
    assert s.record_stop(None) is None


def test_transcribe_missing_file_is_empty_never_raises():
    assert s.transcribe("does-not-exist.wav") == ""


def test_record_start_launches_ffmpeg(monkeypatch):
    seen = {}
    class FakeProc:
        pass
    monkeypatch.setattr(s, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(s, "_first_dshow_device", lambda: "Fake Mic")
    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()
    monkeypatch.setattr(s.subprocess, "Popen", fake_popen)
    p = s.record_start()
    assert isinstance(p, FakeProc)
    assert "dshow" in seen["cmd"] and "audio=Fake Mic" in seen["cmd"]


def test_record_start_none_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(s, "_ffmpeg", lambda: None)
    assert s.record_start() is None


def test_record_stop_sends_q_then_returns_path_when_wav_exists(monkeypatch, tmp_path):
    wav = tmp_path / "r.wav"; wav.write_bytes(b"x" * 2000)
    monkeypatch.setattr(s, "_WAV", wav)
    class P:
        def __init__(self): self.stdin = self
        def write(self, b): P.wrote = b
        def flush(self): pass
        def wait(self, timeout=None): pass
    assert s.record_stop(P()) == str(wav)
    assert P.wrote == b"q"
