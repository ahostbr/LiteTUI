from litetui import optional_python as p


def test_system_dependencies_win_over_venv(monkeypatch):
    monkeypatch.setattr(p, 'candidates', lambda: ['system', 'venv'])
    monkeypatch.setattr(p, 'supports', lambda exe, modules: True)
    assert p.resolve('edge_tts', 'playsound') == 'system'
    assert p.install_target() == 'system'


def test_existing_venv_is_fallback(monkeypatch):
    monkeypatch.setattr(p, 'candidates', lambda: ['system', 'venv'])
    monkeypatch.setattr(p, 'supports', lambda exe, modules: exe == 'venv')
    assert p.resolve('edge_tts', 'playsound') == 'venv'


def test_missing_dependencies_are_not_claimed(monkeypatch):
    monkeypatch.setattr(p, 'candidates', lambda: ['system', 'venv'])
    monkeypatch.setattr(p, 'supports', lambda exe, modules: False)
    assert p.resolve('edge_tts') is None


def test_speech_uses_resolved_interpreter(monkeypatch):
    from litetui import voice_backend as v
    from pathlib import Path
    captured = {}
    monkeypatch.setattr(p, 'resolve', lambda *modules: 'system-python')
    monkeypatch.setattr(v, '_speaker', lambda exe: (exe, 0))
    class Process:
        def wait(self, **kwargs): pass
    def launch(argv, **kwargs):
        captured['argv'] = argv
        captured['source'] = Path(argv[1]).read_text(encoding='utf-8')
        return Process()
    monkeypatch.setattr(v.subprocess, 'Popen', launch)
    assert v.speak('hello', engine='edge')
    assert captured['argv'][0] == 'system-python'
    assert 'edge_tts' in captured['source']
