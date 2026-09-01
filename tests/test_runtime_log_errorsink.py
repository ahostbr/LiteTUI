"""The error sink (T137): where raw detail goes when chat must not have it."""
from pathlib import Path

import pytest

from litetui import runtime_log
from litetui.runtime_log import RuntimeRecorder, default_errors_path, default_log_path


def test_record_error_keeps_what_the_metadata_sink_refuses(tmp_path: Path) -> None:
    """Rule c's two halves in one assertion pair.

    The metadata sanitizer rejects any string with spaces (token regex), so a
    raw exception line can NEVER ride into runtime.jsonl — and record() drops
    the whole event silently rather than leak it. record_error is the seam
    that keeps exactly that text, beside the metadata file.
    """
    path = tmp_path / ".logs" / "runtime.jsonl"
    recorder = RuntimeRecorder(path)
    raw = "urllib.error.URLError: <urlopen error [WinError 10061] refused>"

    # The old pattern — raw text as a metadata value — is rejected, whole event.
    assert recorder.write({"event": "probe", "error": raw}) is False

    # The new seam keeps it.
    assert recorder.write_error("probe", detail=raw) is True
    recorder.close()

    errors = (tmp_path / ".logs" / "runtime-errors.log").read_text(encoding="utf-8")
    assert raw in errors
    # The rejected write never opened the delayed handler, so the file may not
    # exist at all — either way the raw text is NOT in it.
    metadata = path.read_text(encoding="utf-8") if path.exists() else ""
    assert raw not in metadata


def test_error_sink_lands_beside_the_metadata_sink(tmp_path: Path) -> None:
    """One .logs/ to find, two files inside — a reader debugging a crash does
    not have to guess where the raw detail went."""
    log = default_log_path(tmp_path)
    errors = default_errors_path(tmp_path)
    assert errors.parent == log.parent
    assert errors.name == "runtime-errors.log"

    recorder = RuntimeRecorder(log)
    try:
        assert recorder.errors_path == errors
        assert recorder.write_error("probe", detail="raw detail line") is True
    finally:
        recorder.close()
    # Only the error sink was fed, so only its file must exist — but it lands
    # in the SAME .logs/ as the metadata path would.
    assert errors.exists()
    assert errors.parent == log.parent


def test_record_error_carries_traceback_and_metadata(tmp_path: Path) -> None:
    path = tmp_path / ".logs" / "runtime.jsonl"
    recorder = RuntimeRecorder(path)
    try:
        try:
            raise ValueError("the model key is not on the server")
        except ValueError as e:
            assert recorder.write_error(
                "model_load_failed", detail="load refused", exc=e, backend="lmstudio"
            ) is True
    finally:
        recorder.close()

    errors = (tmp_path / ".logs" / "runtime-errors.log").read_text(encoding="utf-8")
    assert "event=model_load_failed" in errors
    assert '"backend": "lmstudio"' in errors
    assert "load refused" in errors
    assert "ValueError: the model key is not on the server" in errors


def test_detail_is_capped_at_one_blob(tmp_path: Path) -> None:
    """A spawned server's whole console tail is legitimate; a multi-megabyte
    body is not. The cap bounds ONE entry, rotation bounds the file."""
    path = tmp_path / ".logs" / "runtime.jsonl"
    recorder = RuntimeRecorder(path)
    try:
        assert recorder.write_error("probe", detail="x" * 20000) is True
    finally:
        recorder.close()

    errors = (tmp_path / ".logs" / "runtime-errors.log").read_text(encoding="utf-8")
    assert "truncated at" in errors
    # The cap plus its marker, not the full blob.
    assert len(errors) < 17000


def test_no_installed_sink_is_a_safe_noop() -> None:
    """Both seams degrade to False without raising — diagnostics must never
    take down the chat, and a module imported before install() is common in
    tests."""
    saved = runtime_log._ACTIVE
    runtime_log._ACTIVE = None
    try:
        assert runtime_log.record("probe", site="test") is False
        assert runtime_log.record_error("probe", detail="raw text here") is False
    finally:
        runtime_log._ACTIVE = saved
