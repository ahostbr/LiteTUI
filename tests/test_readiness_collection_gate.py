"""Collection accounting without invoking pytest collection or test children."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace


def load_gate():
    path = Path(__file__).with_name("readiness_collection_gate.py")
    spec = importlib.util.spec_from_file_location("readiness_collection_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_omitted_selected_file_fails_collection_gate(tmp_path):
    gate = load_gate()
    a, b = tmp_path / "test_a.py", tmp_path / "test_b.py"
    missing = gate.uncollected_files([str(a), str(b)], [SimpleNamespace(path=a)], tmp_path)
    assert missing == [str(b.resolve())]


def test_selected_files_with_items_pass(tmp_path):
    gate = load_gate()
    a = tmp_path / "test_a.py"
    assert gate.uncollected_files([str(a)], [SimpleNamespace(path=a)], tmp_path) == []


def test_node_selection_maps_to_file(tmp_path):
    gate = load_gate()
    a = tmp_path / "test_a.py"
    assert gate.uncollected_files([str(a) + "::test_one"], [SimpleNamespace(path=a)], tmp_path) == []


def test_collection_hook_refuses_missing_items(tmp_path):
    import pytest
    gate = load_gate()
    a = tmp_path / "test_empty.py"
    session = SimpleNamespace(config=SimpleNamespace(args=[str(a)], invocation_params=SimpleNamespace(dir=tmp_path)), items=[])
    with pytest.raises(pytest.UsageError, match="zero collected items"):
        gate.pytest_collection_finish(session)
