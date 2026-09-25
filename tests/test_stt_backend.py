"""stt_backend — voice-in core. No real mic or model touched."""
import pytest

from litetui import stt_backend as s


def test_list_mics_returns_a_list():
    assert isinstance(s.list_mics(), list)


def test_record_stop_none_is_none():
    assert s.record_stop(None) is None


def test_transcribe_worker_failure_propagates(monkeypatch):
    monkeypatch.setattr(s, "_interpreter", lambda: "C:\\fake\\python.exe")
    monkeypatch.setattr(s, "_check_not_silent", lambda p: None)
    def boom(exe, w, m):
        raise RuntimeError("simulated worker crash")
    monkeypatch.setattr(s, "_run_worker", boom)
    with pytest.raises(RuntimeError, match="simulated worker crash"):
        s.transcribe("does-not-exist.wav")


def test_transcribe_without_interpreter_reports_failure(monkeypatch, tmp_path):
    wav = tmp_path / "a.wav"; wav.write_bytes(b"x" * 4000)
    monkeypatch.setattr(s, "_check_not_silent", lambda p: None)
    monkeypatch.setattr(s, "_interpreter", lambda: None)
    calls = {}
    monkeypatch.setattr(s, "_run_worker",
                        lambda exe, w, m: calls.setdefault("called", True))
    with pytest.raises(RuntimeError, match="interpreter is unavailable"):
        s.transcribe(str(wav))
    assert "called" not in calls  # no worker may be launched without an interpreter


def test_run_worker_bad_interpreter_raises():
    with pytest.raises(OSError):
        s._run_worker("C:\\no-such-python.exe", "x.wav", "base.en")


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
    proc = P()
    proc._litetui_wav = wav
    assert s.record_stop(proc) == str(wav)
    assert P.wrote == "q"


def test_hallucinations_are_dropped_speech_is_kept(monkeypatch, tmp_path):
    wav = tmp_path / "s.wav"; wav.write_bytes(b"x" * 4000)
    monkeypatch.setattr(s, "_check_not_silent", lambda p: None)  # not silent
    monkeypatch.setattr(s, "_interpreter", lambda: "C:\\fake\\python.exe")

    outs = iter(["You", "add a login page"])
    monkeypatch.setattr(s, "_run_worker", lambda exe, w, m: next(outs))

    assert s.transcribe(str(wav)) == ""           # stock hallucination -> dropped
    assert s.transcribe(str(wav)) == "add a login page"  # real speech -> kept


def test_missing_and_available_agree(monkeypatch):
    monkeypatch.setattr(s.optional_python, "resolve", lambda *m: "C:\\py.exe")
    monkeypatch.setattr(s, "_ffmpeg", lambda: "ffmpeg")
    assert s.available() and s.missing() == ""
    monkeypatch.setattr(s.optional_python, "resolve", lambda *m: None)
    assert not s.available() and s.missing() == "faster-whisper"
    monkeypatch.setattr(s, "_ffmpeg", lambda: None)
    assert s.missing() == "faster-whisper + ffmpeg"


def test_silent_clip_returns_empty(monkeypatch, tmp_path):
    wav = tmp_path / "q.wav"; wav.write_bytes(b"x" * 4000)
    monkeypatch.setattr(s, "_check_not_silent", lambda p: "[listen] silence")
    assert s.transcribe(str(wav)) == ""


def test_recordings_have_distinct_owned_files(monkeypatch, tmp_path):
    monkeypatch.setattr(s.paths, 'data_root', lambda: tmp_path)
    monkeypatch.setattr(s, '_ffmpeg', lambda: 'ffmpeg')
    monkeypatch.setattr(s, '_first_dshow_device', lambda: 'mic')
    calls = []
    class P:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
    monkeypatch.setattr(s.subprocess, 'Popen', P)
    a, b = s.record_start(), s.record_start()
    assert calls[0][-1] != calls[1][-1]
    assert str(a._litetui_wav) == calls[0][-1]
    assert str(b._litetui_wav) == calls[1][-1]


def test_unknown_process_never_returns_stale_shared_audio(monkeypatch, tmp_path):
    stale = tmp_path / 'stt-record.wav'
    stale.write_bytes(b'x' * 2000)
    monkeypatch.setattr(s, '_WAV', stale)
    class P:
        stdin = None
        def wait(self, **kw): pass
    assert s.record_stop(P()) is None


def test_unconfirmed_recorder_exit_does_not_return_audio(tmp_path):
    wav = tmp_path / 'active.wav'
    wav.write_bytes(b'x' * 2000)
    class P:
        _litetui_wav = wav
        stdin = None
        def wait(self, **kw): raise TimeoutError()
        def kill(self): pass
    assert s.record_stop(P()) is None
