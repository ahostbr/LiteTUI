"""T617: collection and silent readers must not depend on live services."""
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace


def _load_rpc(monkeypatch):
    import urllib.request
    probes = []
    monkeypatch.delenv("LITETUI_E2E", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: probes.append(a))
    spec = importlib.util.spec_from_file_location("rpc_isolation_subject", Path(__file__).with_name("test_rpc.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, probes


def test_collection_never_probes_backend(monkeypatch):
    _, probes = _load_rpc(monkeypatch)
    assert probes == []


def test_silent_reader_deadline_is_real(monkeypatch):
    module, _ = _load_rpc(monkeypatch)
    released = threading.Event()

    class Silent:
        def readline(self):
            released.wait(0.5)
            return ""

    session = module.RpcSession.__new__(module.RpcSession)
    session.proc = SimpleNamespace(stdout=Silent())
    session.events = []
    if hasattr(session, "_start_reader"):
        session._start_reader()
    started = time.monotonic()
    try:
        assert session.read_until(lambda e: True, timeout=0.03) is None
        assert time.monotonic() - started < 0.25
    finally:
        released.set()


def test_child_environment_isolated_and_integrations_disabled(tmp_path, monkeypatch):
    module, _ = _load_rpc(monkeypatch)
    env = module._child_env(tmp_path / "data")
    from litetui.settings import load
    settings = load(tmp_path / "data")
    assert env["LITETUI_DATA_ROOT"] == str(tmp_path / "data")
    assert env["LITETUI_NO_HARNESS"] == "1"
    assert settings.mcp_enabled is False
    assert "scheduler" in settings.plugins_disabled


def test_scripts_only_uses_classifier_without_pytest(monkeypatch):
    from types import SimpleNamespace

    import run_all
    calls = []
    monkeypatch.setattr(run_all.sys, "argv", ["run_all.py", "--scripts-only"])
    monkeypatch.setattr(run_all, "classify", lambda: ([Path("test_py.py")], [Path("script_a.py"), Path("script_b.py")]))
    monkeypatch.setattr(run_all.subprocess, "run", lambda args, **kwargs:
                        calls.append(args) or SimpleNamespace(returncode=0))
    assert run_all.main() == 0
    assert [args[-1] for args in calls] == ["script_a.py", "script_b.py"]
