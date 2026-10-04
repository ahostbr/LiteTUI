"""Standalone conversation search shares LiteTUI's portable data location."""
import importlib.util
from pathlib import Path


def load_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "convo_search.py"
    spec = importlib.util.spec_from_file_location("convo_search_path_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_search_uses_data_override_and_creates_index_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    tool = load_tool()
    assert Path(tool.ROOT) == tmp_path / ".convos"
    assert Path(tool.DB_PATH) == tmp_path / "tools" / "convo_search.db"
    with tool.open_db() as db:
        assert db.execute("SELECT count(*) FROM convos").fetchone() == (0,)
    assert Path(tool.DB_PATH).is_file()


def test_search_default_tracks_checkout_not_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("LITETUI_DATA_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    tool = load_tool()
    root = Path(__file__).resolve().parents[1]
    assert Path(tool.ROOT) == root / ".convos"
    assert Path(tool.DB_PATH) == root / "tools" / "convo_search.db"
