"""Registration diagnostics retain sanitized frame chains, never payloads."""

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


@pytest.mark.parametrize("owned", [True])
def test_actual_register_records_complete_external_chain_without_payload(
    recorded, monkeypatch, owned
):
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    private = "CREDENTIAL_PAYLOAD_MUST_NOT_BE_LOGGED"
    frames = "".join(
        f'  File "registry.py", line {i + 1}, in read_row\n    request = "{private}"\n'
        for i in range(120)
    )
    stderr = (
        "Traceback (most recent call last):\n"
        + frames
        + f"PermissionError: [WinError 32] [Errno 13] {private}\n\n"
        + "The above exception was the direct cause of the following exception:\n\n"
        + "Traceback (most recent call last):\n"
        + '  File "strict_registration.py", line 54, in validate\n'
        + f'    raise StoreError("{private}") from exc\n'
        + f"StoreError: {private}\n"
    )
    result = SimpleNamespace(returncode=1, stdout="", stderr=stderr)
    seat = SimpleNamespace(
        _agent_session=object() if owned else None,
        name="Fixture",
        registered=False,
        _register_as=lambda _: (result, "Fixture"),
        error=None,
    )
    assert not harness.Seat.register(seat)
    metadata = read_metadata(recorded)
    assert metadata["stage"] == "presence-subprocess"
    assert metadata["returncode"] == 1
    assert metadata["parse_failed"] is False
    first, second = metadata["exceptions"]
    assert first["type"] == "PermissionError"
    assert first["relation"] == "cause"
    assert first["errno"] == 13
    assert first["winerror"] == 32
    assert [frame["line"] for frame in first["frames"]] == list(range(1, 121))
    assert second["type"] == "StoreError"
    assert second["frames"][0]["function"] == "validate"
    assert private not in recorded.read_text(encoding="utf-8")
    assert "request =" not in recorded.read_text(encoding="utf-8")
    if owned:
        assert seat.error == diagnostic.OWNED_PRESENCE_FAILURE
        assert private not in seat.error


@pytest.mark.parametrize("owned", [True])
def test_actual_register_exception_records_actual_chained_frames(
    recorded, monkeypatch, owned
):
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
        _agent_session=object() if owned else None,
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
    if owned:
        assert seat.error == diagnostic.OWNED_PRESENCE_FAILURE
        assert private not in seat.error


def test_nonstandard_subprocess_failure_is_explicit_not_payload(recorded):
    diagnostic.record_failure(
        stage="presence-subprocess",
        returncode=7,
        stderr="token SECRET nonstandard fatal output",
    )
    metadata = read_metadata(recorded)
    assert metadata["returncode"] == 7
    assert metadata["parse_failed"] is True
    assert metadata["exceptions"] == [
        {"type": "unknown", "relation": "root", "frames": []}
    ]
    assert "SECRET" not in recorded.read_text(encoding="utf-8")


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
