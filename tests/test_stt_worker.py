"""Exercise the subprocess boundary, without loading Whisper or recording audio."""
import sys
from types import SimpleNamespace

import pytest

from litetui import stt_backend as s
from litetui.app import LiteTUI


def test_real_worker_launches_and_cleans_script(tmp_path, monkeypatch):
    wav = tmp_path / "spoken sentence.wav"
    wav.write_bytes(b"audio fixture")
    monkeypatch.setattr(s, "_STT_CHILD", "import sys; from pathlib import Path; assert Path(sys.argv[1]).read_bytes() == b'audio fixture'; print('spoken sentence')")
    assert s._run_worker(sys.executable, str(wav), "base.en") == "spoken sentence"


def test_worker_failure_reaches_ui_not_no_speech(monkeypatch):
    monkeypatch.setattr(s, "_check_not_silent", lambda p: None)
    monkeypatch.setattr(s, "_interpreter", lambda: sys.executable)

    def fail(*args):
        raise NameError("worker dependency missing")

    monkeypatch.setattr(s, "_run_worker", fail)
    notices, transcripts = [], []
    app = SimpleNamespace(settings=SimpleNamespace(stt_model="base.en"),
                          _system=notices.append, _append_transcript=transcripts.append,
                          call_from_thread=lambda fn, *args: fn(*args))
    LiteTUI._transcribe_and_fill(app, "audio.wav")
    assert transcripts == []
    assert notices == ["[mic] transcription failed: NameError: worker dependency missing"]


def test_child_nonzero_preserves_stderr(monkeypatch):
    monkeypatch.setattr(s, "_STT_CHILD", "import sys; print('decoder failed', file=sys.stderr); sys.exit(3)")
    with pytest.raises(RuntimeError, match="decoder failed"):
        s._run_worker(sys.executable, "audio.wav", "base.en")
