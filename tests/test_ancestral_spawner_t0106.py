"""No-marker relay uses the shared process proof, never inherited identity alone.

The proof is liteharness's process_ancestry.py, vendored byte-identical because
LiteTUI's venv has no liteharness: importing it from liteharness made every live
proof refuse while these arms skipped. Native proof inspects this test process only.
"""
import json
import os
import subprocess
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _owned_seat import owned_app

from litetui import (
    approval_relay,
    harness,
    process_ancestry,
    runtime_log,
    seat_authority,
    tool_policy,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sync_deny_floor

CANDIDATE = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def presence(tmp_path, monkeypatch):
    directory = tmp_path / "presence"
    directory.mkdir()
    monkeypatch.setattr(harness, "AGENTS_DIR", directory)
    return directory / f"{CANDIDATE}.json"


@pytest.fixture
def live_row(presence):
    chain = process_ancestry.process_ancestry(os.getpid(), stop_at=os.getppid())
    assert len(chain) >= 2, "real current-process ancestry must be inspectable for this integration arm"
    parent = chain[1]
    row = {"agent_id": CANDIDATE, "cli": "claude-code", "session_pid": parent.pid,
           "session_process_started_at": parent.started_at * 1000}
    presence.write_text(json.dumps(row), encoding="utf-8")
    return row


def clear_launch(monkeypatch):
    for name in ("LITETUI_SPAWN_IDENTITY", "LITEHARNESS_SPAWNED_BY", "LITETUI_APPROVAL_HOST",
                 "LITETUI_OWNER", "CLAUDE_CODE_SESSION_ID", *seat_authority.AGENT_SHELL_MARKERS):
        monkeypatch.delenv(name, raising=False)


def test_real_no_marker_launch_records_proven_parent_not_ambient_spawned_by(live_row, monkeypatch):
    clear_launch(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CANDIDATE)
    monkeypatch.setenv("LITEHARNESS_SPAWNED_BY", "unrelated-ambient-id")
    app = owned_app()
    assert app._spawned_marker is False and app._owner_seat is False
    assert app._agent_launched is True
    assert app._spawner_id == app.seat.spawned_by == CANDIDATE
    argv = app.seat._presence_argv()
    assert argv[argv.index("--spawned-by") + 1] == CANDIDATE
    assert seat_authority.confirm_route(app) == "spawner"
    assert "LITEHARNESS_SPAWNED_BY" not in os.environ
    assert "LITETUI_APPROVAL_HOST" not in os.environ


def test_marked_envelope_has_priority_even_with_proven_claude_candidate(live_row, monkeypatch):
    clear_launch(monkeypatch)
    monkeypatch.setenv("LITETUI_SPAWN_IDENTITY", "1")
    monkeypatch.setenv("LITEHARNESS_SPAWNED_BY", "marked-parent")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CANDIDATE)
    app = owned_app()
    assert app._spawner_id == app.seat.spawned_by == "marked-parent"
    assert "LITETUI_SPAWN_IDENTITY" not in os.environ
    assert "LITEHARNESS_SPAWNED_BY" not in os.environ


@pytest.mark.parametrize('candidate', [None, '', '.', '..', '../outside', r'..\outside',
                                       '/absolute', 'C:outside', 'bad id', '-option', 42])
def test_invalid_identity_cannot_select_a_presence_path(presence, candidate):
    assert harness.ancestral_claude_spawner(candidate) is None


@pytest.mark.parametrize('change', [
    {"agent_id": "different"}, {"cli": "litetui"}, {"cli": None},
    {"session_pid": True}, {"session_pid": "123"}, {"session_pid": 0},
    {"session_pid": -1}, {"session_pid": 0xFFFFFFFF},
    {"session_process_started_at": True}, {"session_process_started_at": "1000"},
    {"session_process_started_at": 1}, {"session_process_started_at": float('nan')},
    {"session_process_started_at": float('inf')},
])
def test_wrong_type_identity_unrelated_pid_and_stale_birth_refuse(presence, live_row, change):
    presence.write_text(json.dumps({**live_row, **change}), encoding="utf-8")
    assert harness.ancestral_claude_spawner(CANDIDATE) is None


def test_ancestry_is_strict_not_self(presence, live_row):
    presence.write_text(json.dumps({**live_row, "session_pid": os.getpid()}), encoding="utf-8")
    assert harness.ancestral_claude_spawner(CANDIDATE) is None


@pytest.mark.parametrize('value', [[], None, 'invalid', 12])
def test_non_record_presence_refuses(presence, value):
    presence.write_text(json.dumps(value), encoding="utf-8")
    assert harness.ancestral_claude_spawner(CANDIDATE) is None


def test_legacy_registration_timestamp_must_belong_to_observed_process(presence, live_row):
    birth = live_row.pop("session_process_started_at") / 1000
    live_row["registered_at"] = datetime.now(UTC).isoformat()
    presence.write_text(json.dumps(live_row), encoding="utf-8")
    assert harness.ancestral_claude_spawner(CANDIDATE) == CANDIDATE
    for stamp in [datetime.fromtimestamp(birth - 1, UTC).isoformat(),
                  "9999-01-01T00:00:00+00:00", "2026-01-01T00:00:00", "unknown", None]:
        presence.write_text(json.dumps({**live_row, "registered_at": stamp}), encoding="utf-8")
        assert harness.ancestral_claude_spawner(CANDIDATE) is None


def test_unreadable_and_changed_presence_refuse(presence, live_row, monkeypatch):
    read = Path.read_text
    reads = 0

    def changing(path, *args, **kwargs):
        nonlocal reads
        if path == presence:
            reads += 1
            if reads > 1:
                return '{}'
        return read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', changing)
    assert harness.ancestral_claude_spawner(CANDIDATE) is None
    presence.unlink()
    assert harness.ancestral_claude_spawner(CANDIDATE) is None


def test_proof_passes_with_liteharness_not_importable(tmp_path):
    """The shipped venv's condition: no liteharness. A live parent must still be proven."""
    probe = (
        "import json, os, sys; sys.modules['liteharness'] = None\n"
        "from pathlib import Path\n"
        "from litetui import harness, process_ancestry\n"
        "parent = process_ancestry.process_ancestry(os.getpid(), stop_at=os.getppid())[1]\n"
        f"harness.AGENTS_DIR = Path({str(tmp_path)!r})\n"
        f"(harness.AGENTS_DIR / '{CANDIDATE}.json').write_text(json.dumps({{'agent_id': '{CANDIDATE}',"
        " 'cli': 'claude-code', 'session_pid': parent.pid,"
        " 'session_process_started_at': parent.started_at * 1000}), encoding='utf-8')\n"
        f"print(harness.ancestral_claude_spawner('{CANDIDATE}'))\n"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == CANDIDATE, out.stderr


def test_the_copy_is_byte_identical_to_liteharness():
    source = sync_deny_floor.canonical(name="process_ancestry.py")
    if source is None:
        pytest.skip("no liteharness-oss checkout found and LITEHARNESS_SRC unset")
    assert source.is_file(), f"{source} does not exist; set LITEHARNESS_SRC to a checkout that has it"
    warnings.warn(f"process_ancestry.py compared against {source}", stacklevel=1)
    assert source.read_bytes() == Path(process_ancestry.__file__).read_bytes(), (
        f"{source} and src/litetui/process_ancestry.py differ: edit the canonical file "
        "and run scripts/sync_deny_floor.py")


@pytest.mark.asyncio
async def test_unproven_no_marker_launch_preserves_no_spawner_refusal_and_log(presence, monkeypatch, tmp_path):
    clear_launch(monkeypatch)
    monkeypatch.setenv('CLAUDECODE', '1')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', CANDIDATE)
    app = owned_app()
    assert app._spawner_id is None and app.seat.spawned_by is None
    app.settings.tool_policy_profile = tool_policy.INTERACTIVE
    app._active_tool_profile = tool_policy.INTERACTIVE
    app._hook_source = 'typed'
    logs = []
    monkeypatch.setattr(runtime_log, 'record', lambda event, **data: logs.append((event, data)) or True)
    result = await app._authorize_action('bash', {'command': 'rm -rf ./build'},
                                         tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert result[1] is False and app._stop_requested
    assert [data['status'] for event, data in logs if event == 'approval_relay'] == ['no_spawner']
    assert approval_relay.current_spawner(app) is None
