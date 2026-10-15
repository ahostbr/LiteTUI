"""P0 discovery is reproducible source evidence, never a runtime parity pass."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import rust_frontend_inventory as inventory


def sources():
    return {
        "src/litetui/gui_rpc.py": 'PROTOCOL_VERSION = 1\nOPERATIONS = ("hello", "state")\n',
        "src/litetui/plugins/__init__.py": 'PLUGIN_LOAD_ORDER: tuple = ("second", "first")\n',
        "src/litetui/llm_backend.py": 'BACKENDS = (("offline", "Offline fixture"),)\n',
        "src/litetui/rpc.py": (
            'def _dispatch(app, cmd):\n'
            '    cmd_type = cmd.get("type")\n'
            '    if cmd_type == "shutdown": pass\n'
            '    elif "prompt" == cmd_type: pass\n'
            '    elif cmd_type in ("answer", "approve"): pass\n'
            'def unrelated(cmd_type):\n'
            '    if cmd_type == "not-a-command": pass\n'
        ),
    }


def rows(result, category):
    return [r for r in result["records"] if r["category"] == category]


def test_literal_registries_order_and_dispatch_scope():
    result = inventory.build_inventory(sources(), "fixture-revision")
    assert [r["name"] for r in rows(result, "gui_operation")] == ["gui.hello", "gui.state"]
    assert [r["name"] for r in rows(result, "plugin")] == ["second", "first"]
    assert [r["details"]["ordinal"] for r in rows(result, "plugin")] == [0, 1]
    assert [r["name"] for r in rows(result, "rpc_command")] == ["shutdown", "prompt", "answer", "approve"]
    assert result["protocol_version"] == 1


def test_no_source_execution_and_no_string_false_positives():
    files = sources()
    files["src/litetui/specimen.py"] = '''
raise RuntimeError("must never import")
# rpc_emit({"type": "comment"})
text = 'rpc_emit({"type": "string"})'
class Outer:
    class Panel(unknown.Base[str]):
        BINDINGS = [Binding("escape", "close"), *SHARED_KEYS]
        def emit(self):
            self._rpc_emit({**metadata, "type": "text_delta", "text": token})
            rpc_emit(payload)
'''
    result = inventory.build_inventory(files, "rev")
    assert [r["name"] for r in rows(result, "class_candidate")] == ["Outer", "Outer.Panel"]
    binding = rows(result, "bindings")[0]
    assert binding["source"]["symbol"] == "Outer.Panel"
    assert "SHARED_KEYS" in binding["details"]["expression"]
    events = rows(result, "event_emitter")
    assert [r["name"] for r in events] == ["text_delta", "<dynamic>"]
    assert events[0]["source"]["symbol"] == "Outer.Panel.emit"
    assert events[0]["details"]["fields"] == {"type": "'text_delta'", "text": "token"}
    assert events[0]["details"]["unresolved"] == ["**metadata"]
    assert events[1]["details"]["unresolved"] == ["payload"]


def test_duplicate_occurrences_are_preserved_and_reported():
    files = sources()
    files["src/litetui/gui_rpc.py"] = 'PROTOCOL_VERSION = 1\nOPERATIONS = ("hello", "hello")\n'
    result = inventory.build_inventory(files, "rev")
    assert len(rows(result, "gui_operation")) == 2
    assert len({r["id"] for r in result["records"]}) == len(result["records"])
    assert any("duplicate" in d for d in result["diagnostics"])


@pytest.mark.parametrize("replacement", ['OPERATIONS = discover()\n', 'OPERATIONS = (1,)\n', 'unrelated = ()\n'])
def test_required_registry_cannot_silently_disappear(replacement):
    files = sources()
    files["src/litetui/gui_rpc.py"] = 'PROTOCOL_VERSION = 1\n' + replacement
    with pytest.raises(ValueError, match="OPERATIONS"):
        inventory.build_inventory(files, "rev")


def test_syntax_error_names_source_and_aborts():
    files = sources()
    files["src/litetui/broken.py"] = 'def broken(:\n'
    with pytest.raises(SyntaxError, match="broken.py"):
        inventory.build_inventory(files, "rev")


def test_determinism_hashes_and_test_candidates_are_not_results():
    files = sources()
    files["tests/test_specimen.py"] = 'def test_unicode():\n    assert "雪"\n'
    first = inventory.build_inventory(files, "rev")
    second = inventory.build_inventory(dict(reversed(list(files.items()))), "rev")
    assert inventory.serialize(first) == inventory.serialize(second)
    assert first["tests"] == [{"path": "tests/test_specimen.py", "status": "not_run"}]
    assert first["evidence_kind"] == "static_source"
    assert all(r["status"] == "unverified" for r in first["records"])
    files["tests/test_specimen.py"] += '# change\n'
    assert inventory.build_inventory(files, "rev")["inputs"] != first["inputs"]


def test_parity_seed_never_claims_a_verified_contract_or_overwrites_evidence(tmp_path):
    result = inventory.build_inventory(sources(), "rev")
    target = tmp_path / "parity.json"
    inventory.seed_parity(result, target)
    seed = json.loads(target.read_text(encoding="utf-8"))
    assert len(seed["rows"]) == len(result["records"])
    for row in seed["rows"]:
        assert row["status"] == "blocked"
        assert row["request"] is None and row["events"] is None
        assert row["terminals"] == {"litesuite_embedded": "not_run", "windows_terminal": "not_run"}
        assert row["evidence"] == []
    target.write_text('human-owned evidence', encoding="utf-8")
    with pytest.raises(FileExistsError):
        inventory.seed_parity(result, target)
    assert target.read_text(encoding="utf-8") == 'human-owned evidence'


def test_missing_core_file_is_an_error():
    files = sources()
    del files["src/litetui/rpc.py"]
    with pytest.raises(ValueError, match="rpc.py"):
        inventory.build_inventory(files, "rev")


def run_cli(tmp_path, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/rust_frontend_inventory.py"),
         "--root", str(ROOT), *map(str, args)], cwd=tmp_path,
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False,
    )


def test_cli_pinned_blobs_check_and_stale_failure_without_writes(tmp_path):
    output = tmp_path / "static.json"
    command = ("--output", output)
    made = run_cli(tmp_path, *command)
    assert made.returncode == 0, made.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["source_revision"] == inventory.BASELINE
    assert result["counts"]["gui_operation"] == 84
    assert len([i for i in result["inputs"] if i["path"].startswith("src/")]) == 262
    assert not any(i["path"].endswith("test_rust_frontend_inventory.py") for i in result["inputs"])
    before = output.read_bytes()
    checked = run_cli(tmp_path, *command, "--check")
    assert checked.returncode == 0, checked.stderr
    assert output.read_bytes() == before
    output.write_text("stale", encoding="utf-8")
    stale = run_cli(tmp_path, *command, "--check")
    assert stale.returncode != 0 and "stale" in stale.stderr
    assert output.read_text(encoding="utf-8") == "stale"


def test_cli_rejects_same_output_and_parity_seed_before_writing(tmp_path):
    output = tmp_path / "parity.json"
    result = run_cli(tmp_path, "--output", output, "--seed-parity", output)
    assert result.returncode != 0
    assert not output.exists()


def test_cli_bad_revision_does_not_write_output(tmp_path):
    output = tmp_path / "missing.json"
    result = run_cli(tmp_path, "--revision", "not-a-real-revision", "--output", output)
    assert result.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize(("payload", "expected"), [
    ('{"type": "old", "type": kind}', "<dynamic>"),
    ('{"type": "old", **metadata}', "<dynamic>"),
    ('{**metadata, "type": "final"}', "final"),
    ('{"type": "old", "type": "final"}', "final"),
])
def test_event_type_respects_overrides(payload, expected):
    files = sources()
    files["src/litetui/emitter.py"] = f'rpc_emit({payload})\n'
    result = inventory.build_inventory(files, "rev")
    assert rows(result, "event_emitter")[0]["name"] == expected


def test_missing_dispatcher_and_dynamic_membership_are_visible():
    files = sources()
    files["src/litetui/rpc.py"] = 'def renamed(): pass\n'
    with pytest.raises(ValueError, match="_dispatch"):
        inventory.build_inventory(files, "rev")
    files["src/litetui/rpc.py"] = 'def _dispatch(app, cmd):\n    if cmd_type in COMMANDS: pass\n'
    result = inventory.build_inventory(files, "rev")
    assert any("unresolved command comparison" in d for d in result["diagnostics"])


def test_source_reader_uses_blobs_not_export_attributes(tmp_path):
    for path, content in sources().items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_export.py").write_text('revision = "$Format:%H$"\n', encoding="utf-8")
    (tmp_path / ".gitattributes").write_text(
        'src/litetui/gui_rpc.py export-ignore\ntests/test_export.py export-subst\n', encoding="utf-8")
    for args in [("init",), ("add", "."),
                 ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "-c", "core.hooksPath=disabled-hooks", "commit", "-m", "fixture")]:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)
    revision, files = inventory.load_sources(tmp_path, "HEAD")
    assert len(revision) == 40
    assert files["src/litetui/gui_rpc.py"] == sources()["src/litetui/gui_rpc.py"]
    assert files["tests/test_export.py"] == 'revision = "$Format:%H$"\n'


def test_static_write_failure_does_not_publish_parity_seed(tmp_path):
    output = tmp_path / "is-a-directory"
    output.mkdir()
    seed = tmp_path / "parity.json"
    result = run_cli(tmp_path, "--output", output, "--seed-parity", seed)
    assert result.returncode != 0
    assert not seed.exists()


def test_static_output_refuses_to_overwrite_non_inventory_evidence(tmp_path):
    output = tmp_path / "parity.json"
    output.write_text('{"rows": ["reviewed evidence"]}', encoding="utf-8")
    before = output.read_bytes()
    result = run_cli(tmp_path, "--output", output)
    assert result.returncode != 0
    assert output.read_bytes() == before
