"""Connect-only metadata never follows another origin or changes a running model."""

import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from litetui import native_engine_endpoint as endpoint


def test_scoped_requests_use_only_canonical_read_routes(monkeypatch):
    called = Mock(return_value="controlled response")
    monkeypatch.setattr(endpoint, "_open_pinned", called)
    host = "http://127.0.0.1:12345"
    with endpoint.pinned(host):
        assert (
            endpoint.open_read(urllib.request.Request(host + "/health"), timeout=2)
            == "controlled response"
        )
        for url, method in [
            (host + "/v1/unload", "POST"),
            (host + "/health", "POST"),
            ("http://127.0.0.1:54321/v1/models", "GET"),
            (host + "/v1/models?next=external", "GET"),
            (host + "/../v1/models", "GET"),
        ]:
            with pytest.raises(ValueError, match="outside"):
                endpoint.open_read(
                    urllib.request.Request(url, method=method), timeout=2
                )
    assert called.call_count == 1
    assert (
        endpoint._NoRedirect().redirect_request(
            None, None, 302, "", {}, host + "/elsewhere"
        )
        is None
    )


def test_action_selects_only_existing_loopback_endpoint():
    choice = Mock(return_value="http://127.0.0.1:12345")
    settings = SimpleNamespace(
        engine_management=True, validate_engine_management=Mock()
    )
    app = SimpleNamespace(
        backend=SimpleNamespace(name="ninfer", host=choice),
        model_id="a",
        _launch_options=settings,
    )
    assert endpoint.selection(app)["acquire_handle"] is False
    for invalid in [
        "http://localhost:1234x",
        "http://localhost:01234",
        "http://foreign.example:123",
        "http://localhost:123/a",
        "http://u@127.0.0.1:123",
        "https://localhost",
    ]:
        choice.return_value = invalid
        with pytest.raises(ValueError):
            endpoint.selection(app)
