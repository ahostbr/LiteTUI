"""Always-on rotating runtime diagnostics, metadata only."""
from pathlib import Path

import pytest

from litetui.plugins import runtime_log_plugin
from litetui.runtime_log import (
    MetadataRejected,
    RuntimeRecorder,
    sanitize_event,
)


def test_metadata_event_is_serializable() -> None:
    clean = sanitize_event(
        {
            "event": "tool_finished",
            "name": "bash",
            "duration_ms": 125.5,
            "exit_code": 0,
            "ok": True,
        }
    )
    assert clean["event"] == "tool_finished"
    assert clean["name"] == "bash"


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "bad", "prompt": "secret prompt"},
        {"event": "bad", "arguments": "--token secret"},
        {"event": "bad", "result": "private tool output"},
        {"event": "bad", "conversation": "private conversation"},
        {"event": "bad", "label": "private prompt passed as a label"},
        {"event": "bad", "site": {"body": "nested secret"}},
    ],
)
def test_bodies_cannot_cross_the_metadata_sanitizer(payload: dict) -> None:
    with pytest.raises(MetadataRejected):
        sanitize_event(payload)


def test_unknown_key_and_overlong_string_fail_closed() -> None:
    with pytest.raises(MetadataRejected, match="unknown metadata key"):
        sanitize_event({"event": "probe", "surprise": 3})
    with pytest.raises(MetadataRejected, match="too long"):
        sanitize_event({"event": "probe", "site": "x" * 200})


def test_recorder_rotates_and_keeps_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "runtime.jsonl"
    recorder = RuntimeRecorder(path, max_bytes=180, backup_count=2)
    for index in range(12):
        recorder.write({"event": "probe", "count": index, "site": "test"})
    recorder.close()
    assert path.exists()
    assert path.with_suffix(".jsonl.1").exists()
    assert path.stat().st_size <= 180


def test_signal_adapter_drops_labels_instead_of_logging_bodies(tmp_path: Path) -> None:
    recorder = RuntimeRecorder(tmp_path / "runtime.jsonl")
    recorder.write_signal(
        {"channel": "output", "intensity": 0.5, "label": "private answer body"}
    )
    recorder.close()
    logged = (tmp_path / "runtime.jsonl").read_text(encoding="utf-8")
    assert "private answer body" not in logged
    assert '"channel":"output"' in logged


def test_first_party_plugin_is_always_on_without_a_command(tmp_path: Path) -> None:
    observed = []

    class Context:
        def observe(self, handler) -> None:
            observed.append(handler)

    runtime_log_plugin.register(Context(), root=tmp_path)
    assert len(observed) == 1
    observed[0]({"channel": "context", "intensity": 1.0, "label": "private"})
    assert runtime_log_plugin._RECORDER is not None
    runtime_log_plugin._RECORDER.close()
    logged = (tmp_path / ".logs" / "runtime.jsonl").read_text(encoding="utf-8")
    assert '"event":"signal"' in logged
    assert "private" not in logged
