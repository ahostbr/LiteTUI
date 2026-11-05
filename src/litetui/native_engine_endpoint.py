"""Pin the destination of a reviewed, connect-only engine operation."""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import io
import urllib.request
from urllib.parse import urlsplit

from litetui.background_input import encoded

OPERATION = "gui.engine.native_reconnect_validate"
_current = contextvars.ContextVar("native_engine_endpoint", default=None)


def selection(app):
    from litetui import native_engine

    if not native_engine.requested(app):
        raise ValueError("An explicitly managed engine is required")
    app._launch_options.validate_engine_management(app.backend.name, app.model_id)
    endpoint = app.backend.host().rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
        or not parsed.port
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or len(endpoint) > 512
    ):
        raise ValueError(
            "A scoped reconnect requires a configured loopback host and explicit port"
        )
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    if endpoint != f"{parsed.scheme}://{host}:{parsed.port}":
        raise ValueError(
            "Use an exact loopback address without additional URL components"
        )
    result = {
        "endpoint": endpoint,
        "model": app.model_id,
        "start_process": False,
        "load_model": False,
        "acquire_handle": False,
    }
    result["selection_id"] = hashlib.sha256(encoded(result)).hexdigest()
    return result


@contextlib.contextmanager
def pinned(endpoint):
    token = _current.set(endpoint)
    try:
        yield
    finally:
        _current.reset(token)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_pinned(req, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(req, timeout=timeout) as response:
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Engine metadata exceeds its bounded read limit")
        body = io.BytesIO(content)
        body.status = response.status
        return body


def open_read(req, *, timeout):
    endpoint = _current.get()
    if endpoint is None:
        return urllib.request.urlopen(req, timeout=timeout)
    if req.get_method() != "GET" or req.full_url not in {
        endpoint + "/health",
        endpoint + "/v1/models",
    }:
        raise ValueError(
            "Engine reconnect attempted a request outside its reviewed endpoint"
        )
    return _open_pinned(req, timeout)


async def check_served(backend, reviewed):

    if backend._owned is not None or backend.host() != reviewed["endpoint"]:
        raise ValueError("Engine endpoint or process ownership changed")
    if not await asyncio.to_thread(backend._health, reviewed["endpoint"]):
        raise ValueError(
            "Reviewed engine endpoint is offline or has unloaded its model"
        )
    rows = await backend.list_models()
    if len(rows) != 1 or rows[0].key != reviewed["model"] or not rows[0].loaded:
        raise ValueError(
            "Reviewed endpoint is not already serving exactly the selected loaded model"
        )
    await backend.ensure_chat_ready(reviewed["model"])


def verify_connection(app, reviewed):
    binding = getattr(app, "_client_binding", None)
    if (
        app.backend._owned is not None
        or app.backend.host() != reviewed["endpoint"]
        or binding is None
        or binding.client is not app.client
        or binding.backend_type is not type(app.backend)
        or binding.endpoint != reviewed["endpoint"] + "/v1"
        or str(app.client.base_url).rstrip("/") != binding.endpoint
    ):
        raise ValueError(
            "Reconnected client does not own the exact reviewed endpoint and backend"
        )


async def join_probe(operation):
    # Metadata calls use worker threads. Preserve the caller's cancellation after
    # these bounded requests (and any canonical connection worker) have retired.
    from litetui.shutdown_wait import join_task

    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await join_task(task)
        finally:
            raise asyncio.CancelledError()
