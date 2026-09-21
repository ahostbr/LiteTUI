"""Offline packaging-drift gate for tools/wheel_import_gate.check_zip.

Pure synthetic fixtures — a fake source tree plus a fake wheel (a plain zip).
NO real build, NO uv, NO dependency install, NO network, NO skips. Proves
check_zip requires every source module (including an implicit-NAMESPACE module
that has no __init__.py) and every package-data resource (schemas / prompts /
assets *.wav / PI_NOTICE.txt), and rejects a wheel that drops any one of them.

The gate module is loaded by file path so the test needs no `tools` package on
sys.path and no build step.
"""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parents[1] / "tools" / "wheel_import_gate.py"
_spec = importlib.util.spec_from_file_location("wheel_import_gate_under_test", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(gate)


def _make_source(root: Path) -> Path:
    """A minimal but representative src/litetui tree."""
    pkg = root / "litetui"
    (pkg / "monitor").mkdir(parents=True)
    (pkg / "plugins").mkdir()
    (pkg / "prompts").mkdir()
    (pkg / "schemas").mkdir()
    (pkg / "assets").mkdir()
    (pkg / "ns").mkdir()             # implicit NAMESPACE dir: real .py, NO __init__.py
    (pkg / "__pycache__").mkdir()    # generated cache: must be EXCLUDED
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "app.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "monitor" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "monitor" / "cli.py").write_text("", encoding="utf-8")
    (pkg / "plugins" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "plugins" / "p.py").write_text("", encoding="utf-8")
    (pkg / "prompts" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "prompts" / "first.md").write_text("hi", encoding="utf-8")
    (pkg / "schemas" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "schemas" / "tool.json").write_text("{}", encoding="utf-8")
    (pkg / "assets" / "beep.wav").write_bytes(b"RIFF")
    (pkg / "PI_NOTICE.txt").write_text("notice", encoding="utf-8")
    (pkg / "ns" / "ns_mod.py").write_text("", encoding="utf-8")          # namespace module
    (pkg / "__pycache__" / "app.cpython-311.pyc").write_bytes(b"\x00")   # never required
    return pkg


def _make_wheel(path: Path, entries, pkg=None) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for entry in entries:
            data = (pkg.parent / entry).read_bytes() if pkg else b"x"
            zf.writestr(entry, data)
    return path


def test_enumeration_requires_namespace_module_and_excludes_cache(tmp_path):
    pkg = _make_source(tmp_path / "src")
    required = gate._required_entries(pkg)
    # A namespace module (no __init__.py) is required, not silently dropped.
    assert "litetui/ns/ns_mod.py" in required
    # Data resources are all required.
    assert "litetui/PI_NOTICE.txt" in required
    assert "litetui/assets/beep.wav" in required
    assert "litetui/schemas/tool.json" in required
    assert "litetui/prompts/first.md" in required
    # Bytecode caches never appear in the requirement set.
    assert not any("__pycache__" in e or e.endswith(".pyc") for e in required)


def test_complete_wheel_passes(tmp_path):
    pkg = _make_source(tmp_path / "src")
    wheel = _make_wheel(tmp_path / "ok.whl", gate._required_entries(pkg), pkg)
    gate.check_zip(wheel, pkg)  # must not raise


@pytest.mark.parametrize("drop", [
    "litetui/app.py",            # a top-level module
    "litetui/ns/ns_mod.py",      # a NAMESPACE module (no __init__.py)
    "litetui/PI_NOTICE.txt",     # the notice
    "litetui/assets/beep.wav",   # a wav
    "litetui/schemas/tool.json", # a schema
    "litetui/prompts/first.md",  # a prompt
])
def test_wheel_missing_one_required_entry_is_rejected(tmp_path, drop):
    pkg = _make_source(tmp_path / "src")
    required = gate._required_entries(pkg)
    assert drop in required
    wheel = _make_wheel(tmp_path / "bad.whl", [e for e in required if e != drop])
    with pytest.raises(SystemExit):
        gate.check_zip(wheel, pkg)


@pytest.mark.parametrize("changed", ["app.py", "schemas/tool.json", "prompts/first.md"])
def test_stale_wheel_bytes_are_rejected(tmp_path, changed):
    pkg = _make_source(tmp_path / "src")
    wheel = _make_wheel(tmp_path / "stale.whl", gate._required_entries(pkg), pkg)
    (pkg / changed).write_bytes(b"changed after build")
    with pytest.raises(SystemExit):
        gate.check_zip(wheel, pkg)


def test_duplicate_required_zip_member_is_rejected(tmp_path):
    pkg = _make_source(tmp_path / "src")
    wheel = _make_wheel(tmp_path / "duplicate.whl", gate._required_entries(pkg), pkg)
    with zipfile.ZipFile(wheel, "a") as zf:
        with pytest.warns(UserWarning, match="Duplicate name"):
            zf.writestr("litetui/app.py", (pkg / "app.py").read_bytes())
    with pytest.raises(SystemExit):
        gate.check_zip(wheel, pkg)


@pytest.mark.parametrize("extra", ["litetui/obsolete.py", "litetui/schemas/removed.json", "litetui/prompts/removed.md"])
def test_obsolete_package_payload_is_rejected(tmp_path, extra):
    pkg = _make_source(tmp_path / "src")
    wheel = _make_wheel(tmp_path / "obsolete.whl", gate._required_entries(pkg), pkg)
    with zipfile.ZipFile(wheel, "a") as zf:
        zf.writestr(extra, b"obsolete build residue")
    with pytest.raises(SystemExit):
        gate.check_zip(wheel, pkg)


def test_distribution_metadata_is_not_obsolete_package_payload(tmp_path):
    pkg = _make_source(tmp_path / "src")
    wheel = _make_wheel(tmp_path / "metadata.whl", gate._required_entries(pkg), pkg)
    with zipfile.ZipFile(wheel, "a") as zf:
        zf.writestr("litetui-1.0.dist-info/METADATA", b"Name: litetui")
    gate.check_zip(wheel, pkg)


def test_missing_pi_notice_in_source_fails_clearly(tmp_path):
    """A required named source file that is itself absent fails loudly in
    enumeration rather than vanishing from a glob."""
    pkg = _make_source(tmp_path / "src")
    (pkg / "PI_NOTICE.txt").unlink()
    wheel = _make_wheel(tmp_path / "any.whl", ["litetui/app.py"])
    with pytest.raises(SystemExit):
        gate.check_zip(wheel, pkg)
