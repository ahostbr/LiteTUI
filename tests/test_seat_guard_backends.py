"""seat_guard through the backend seam (Sentinel's integration finding:
lms verbs against a llama-served model manage nothing).

The module's own shape — breadcrumb on suspend, cleared on resume, retries,
wrong-context detection — must hold for a backend-driven cycle exactly as it
did for the lms path, and the attached-server refusal must pass through
verbatim so the studio tool can surface it.
"""
from __future__ import annotations

from litetui import seat_guard


class FakeLlamaBackend:
    name = "llamacpp"

    def __init__(self):
        self.loaded = {"seat": {"identifier": "seat", "context": 4096,
                                "parallel": None, "status": "idle", "queued": 0}}
        self.resume_ctx = 4096
        self.refuse = None

    def host(self):
        return "http://localhost:7470"

    def seat_snapshot(self, key):
        return self.loaded.get(key)

    def seat_suspend(self, rec):
        if self.refuse:
            return self.refuse
        self.loaded.pop(rec["identifier"], None)
        return None

    def seat_resume(self, rec):
        got_ctx = self.resume_ctx
        self.loaded[rec["identifier"]] = {
            "identifier": rec["identifier"], "context": got_ctx,
            "parallel": None, "status": "idle", "queued": 0,
        }
        if rec.get("context") and got_ctx != rec["context"]:
            return (f"seat reloaded but at context {got_ctx} "
                    f"instead of {rec['context']}")
        return None


def test_full_cycle_with_breadcrumb(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "crumb.json")
    b = FakeLlamaBackend()
    rec = seat_guard.record("seat", b)
    assert rec["context"] == 4096

    assert seat_guard.suspend(rec, b) is None
    assert seat_guard.BREADCRUMB.exists(), "a suspended seat with no breadcrumb is unrecoverable"
    crumb = seat_guard.BREADCRUMB.read_text(encoding="utf-8")
    assert "/load seat" in crumb, "the recovery hint must match the ENGINE (not an lms line)"

    assert seat_guard.resume(rec, b) is None
    assert not seat_guard.BREADCRUMB.exists(), "a successful resume must clear the crumb"


def test_attached_refusal_passes_through(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "crumb.json")
    b = FakeLlamaBackend()
    b.refuse = "suspend unsupported: LiteSuite owns the server at http://localhost:8088"
    rec = seat_guard.record("seat", b)
    err = seat_guard.suspend(rec, b)
    assert err and "LiteSuite owns" in err
    assert not seat_guard.BREADCRUMB.exists(), "a refused suspend must not leave a crumb"


def test_wrong_context_is_reported_not_hidden(tmp_path, monkeypatch):
    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "crumb.json")
    monkeypatch.setattr(seat_guard.time, "sleep", lambda s: None)
    b = FakeLlamaBackend()
    rec = seat_guard.record("seat", b)
    seat_guard.suspend(rec, b)
    b.resume_ctx = 2048   # the JIT-default trap, llama edition
    err = seat_guard.resume(rec, b)
    assert err and "2048" in err and "4096" in err


def test_unmanageable_seat_records_none(tmp_path, monkeypatch):
    """record() returning None = generate WITHOUT suspending — the studio
    tool's pre-seat-guard behavior, which is the honest fallback."""
    b = FakeLlamaBackend()
    b.loaded.clear()
    assert seat_guard.record("seat", b) is None
