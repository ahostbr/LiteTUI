"""Explicit runner inventory rules over tiny source-only fixture trees."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_all


def test_conditional_tests_and_helpers_are_never_silent_scripts(tmp_path, monkeypatch):
    (tmp_path / "test_cond.py").write_text("if True:\n    def test_ok(): pass\n")
    (tmp_path / "test_helper.py").write_text("def helper(): pass\n")
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    monkeypatch.setattr(run_all, "LEGACY_SCRIPT_TESTS", frozenset())
    pyt, scr = run_all.explicit_inventory()
    assert {p.name for p in pyt} == {"test_cond.py", "test_helper.py"}
    assert scr == []


def test_missing_declared_script_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    monkeypatch.setattr(run_all, "LEGACY_SCRIPT_TESTS", frozenset({"test_missing.py"}))
    with pytest.raises(ValueError, match="missing"):
        run_all.explicit_inventory()


def test_only_explicit_script_is_routed_as_script(tmp_path, monkeypatch):
    script = tmp_path / "test_legacy.py"
    script.write_text("import sys\nsys.exit(0)\n")
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    monkeypatch.setattr(run_all, "LEGACY_SCRIPT_TESTS", frozenset({script.name}))
    assert run_all.explicit_inventory() == ([], [script])


def test_unknown_import_hazard_requires_review(tmp_path, monkeypatch):
    (tmp_path / "test_unknown.py").write_text("import sys\nsys.exit(0)\n")
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    monkeypatch.setattr(run_all, "LEGACY_SCRIPT_TESTS", frozenset())
    with pytest.raises(ValueError, match="review"):
        run_all.explicit_inventory()


def test_syntax_error_is_not_silently_reclassified(tmp_path, monkeypatch):
    (tmp_path / "test_broken.py").write_text("def test_bad(:\n")
    monkeypatch.setattr(run_all, "TESTS", tmp_path)
    monkeypatch.setattr(run_all, "LEGACY_SCRIPT_TESTS", frozenset())
    with pytest.raises(ValueError, match="syntax"):
        run_all.explicit_inventory()
