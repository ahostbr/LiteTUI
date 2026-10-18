"""External payload never enters logs; local actual exception objects are labeled."""

import json
from types import SimpleNamespace

import pytest

from litetui import harness, runtime_log
from litetui import registration_diagnostics as diagnostic


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    recorder = runtime_log.RuntimeRecorder(tmp_path / "runtime.jsonl")
    monkeypatch.setattr(runtime_log, "_ACTIVE", recorder)
    try:
        yield recorder.errors_path
    finally:
        recorder.close()


def read_metadata(path):
    line = next(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("meta=")
    )
    return json.loads(line.removeprefix("meta="))


@pytest.mark.parametrize(
    "payload",
    [
        "FORBIDDEN_PAYLOAD arbitrary output",
        'ValueError: message\n  File "FORBIDDEN_PAYLOAD", line 1, in FORBIDDEN_FUNCTION',
        'ValueError: message\n  File "FORBIDDEN_PAYLOAD", line 1, in FORBIDDEN_FUNCTION\nPermissionError: fake',
        'Traceback (most recent call last):\n  File "FORBIDDEN_PAYLOAD", line 1, in FORBIDDEN_FUNCTION\nPermissionError: [Errno 13] FORBIDDEN_PAYLOAD',
        'ValueError: FORBIDDEN_PAYLOAD\n\nThe above exception was the direct cause of the following exception:\n\nTraceback (most recent call last):\n  File "FORBIDDEN_PAYLOAD", line 1, in FORBIDDEN_FUNCTION\nStoreError: FORBIDDEN_PAYLOAD',
    ],
)
def test_actual_owned_register_never_captures_external_payload(
    recorded, monkeypatch, payload
):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    result = SimpleNamespace(returncode=7, stdout=payload, stderr=payload)
    seat = SimpleNamespace(
        _agent_session=object(),
        name="Fixture",
        registered=False,
        _register_as=lambda _: (result, "Fixture"),
        error=None,
    )
    assert not harness.Seat.register(seat)
    metadata = read_metadata(recorded)
    assert metadata == {
        "stage": "presence-subprocess",
        "returncode": 7,
        "parse_failed": True,
    }
    assert seat.error == diagnostic.OWNED_PRESENCE_FAILURE
    log = recorded.read_text(encoding="utf-8")
    assert "FORBIDDEN_PAYLOAD" not in log
    assert "FORBIDDEN_FUNCTION" not in log
    assert "PermissionError" not in log
    assert "StoreError" not in log
    assert "exceptions" not in metadata


def test_actual_register_exception_records_actual_chained_frames(recorded, monkeypatch):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    private = "PRIVATE_MESSAGE_AND_LOCAL"

    def fail(_):
        try:
            error = PermissionError(13, private)
            error.winerror = 32
            raise error
        except PermissionError as exc:
            raise ValueError(private) from exc

    seat = SimpleNamespace(
        _agent_session=object(),
        name="Fixture",
        registered=False,
        _register_as=fail,
        error=None,
    )
    assert not harness.Seat.register(seat)
    metadata = read_metadata(recorded)
    assert metadata["stage"] == "presence-registration"
    assert metadata["returncode"] is None
    assert metadata["parse_failed"] is False
    assert (
        metadata["format"] == "trusted-in-process-exception-objects-not-external-chain"
    )
    first, second = metadata["exceptions"]
    assert (first["type"], first["errno"], first["winerror"]) == (
        "PermissionError",
        13,
        32,
    )
    assert first["frames"][-1]["function"] == "fail"
    assert second["type"] == "ValueError"
    assert [frame["function"] for frame in second["frames"]] == ["register", "fail"]
    assert private not in recorded.read_text(encoding="utf-8")
    assert seat.error == diagnostic.OWNED_PRESENCE_FAILURE


def test_context_and_exception_group_preserve_actual_frame_objects():
    try:
        try:
            raise PermissionError(13, "private")
        except PermissionError:
            raise RuntimeError("private")
    except RuntimeError as exc:
        chain = diagnostic.exception_chain(exc)
    assert [(entry["type"], entry["relation"]) for entry in chain] == [
        ("PermissionError", "context"),
        ("RuntimeError", "root"),
    ]
    assert all(entry["frames"] for entry in chain)
    group = ExceptionGroup("private", [ValueError("private"), OSError(5, "private")])
    assert [entry["type"] for entry in diagnostic.exception_chain(group)] == [
        "ExceptionGroup",
        "ValueError",
        "OSError",
    ]
