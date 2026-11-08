"""NInfer 2026-11-08: compaction round 3 died with HTTP 500 "invalid OpenAI Chat prompt-progress state" (engine request 15).

With return_progress the engine checks each progress report against the reuse it announced at admission and fails the whole request when
they differ. The readout is a convenience; the request must not be lost for it. The transport resends once without return_progress and
remembers it for the session; any other error is raised as before."""
from types import SimpleNamespace as NS

import pytest

from litetui.model_transport import OpenAITransport


class _Boom(Exception):
    def __init__(self, text):
        super().__init__(text)
        self.body = {"error": {"message": text}}


def _client(first_error=None):
    calls = []

    async def create(**kw):
        calls.append(kw)
        if first_error is not None and len(calls) == 1:
            raise first_error
        return "stream"

    return calls, NS(chat=NS(completions=NS(create=create)))


KW = dict(model="m", messages=[{"role": "user", "content": "hi"}], extra_body={"return_progress": True, "reasoning_effort": "low"})


@pytest.mark.asyncio
async def test_a_prompt_progress_failure_resends_without_return_progress():
    calls, client = _client(_Boom("invalid OpenAI Chat prompt-progress state"))
    backend = NS(name="ninfer")
    out = await OpenAITransport(client, backend=backend).create(purpose="compaction", **KW)
    assert out == "stream" and len(calls) == 2
    assert calls[0]["extra_body"]["return_progress"] is True
    assert "return_progress" not in calls[1]["extra_body"] and calls[1]["extra_body"]["reasoning_effort"] == "low", "only the progress flag goes"
    assert backend._progress_broken is True


@pytest.mark.asyncio
async def test_after_one_failure_later_requests_do_not_ask_for_progress():
    calls, client = _client()
    backend = NS(name="ninfer", _progress_broken=True)
    await OpenAITransport(client, backend=backend).create(purpose="turn", **KW)
    assert len(calls) == 1 and "return_progress" not in calls[0]["extra_body"]


@pytest.mark.asyncio
async def test_other_errors_are_raised_and_progress_stays_on():
    calls, client = _client(_Boom("something else broke"))
    backend = NS(name="ninfer")
    with pytest.raises(_Boom):
        await OpenAITransport(client, backend=backend).create(purpose="turn", **KW)
    assert len(calls) == 1 and not getattr(backend, "_progress_broken", False)


class _Chunk:
    def __init__(self, progress=False, text=None):
        self.prompt_progress = {"processed": 1} if progress else None
        self.model_extra = None
        self.text = text


class _Stream:
    """A stream that yields the given chunks and then raises `error` (if any), like an engine failing mid-response."""

    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.closed = chunks, error, False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error

    async def close(self):
        self.closed = True


def _streaming_client(*streams):
    calls = []

    async def create(**kw):
        calls.append(kw)
        return streams[len(calls) - 1]

    return calls, NS(chat=NS(completions=NS(create=create)))


SKW = dict(KW, stream=True)


@pytest.mark.asyncio
async def test_a_failure_after_the_stream_opened_but_before_any_output_resends_without_return_progress():
    """NInfer request 23 (2026-11-08 12:34): the error came as an event while iterating, so create's own resend never saw it."""
    failing = _Stream([_Chunk(progress=True)], error=_Boom("invalid OpenAI Chat prompt-progress state"))
    good = _Stream([_Chunk(text="summary")])
    calls, client = _streaming_client(failing, good)
    backend = NS(name="ninfer")
    stream = await OpenAITransport(client, backend=backend).create(purpose="compaction", **SKW)
    got = [chunk async for chunk in stream]
    assert [c.text for c in got] == [None, "summary"], "the first stream's progress chunk, then the resent stream's output"
    assert len(calls) == 2 and calls[0]["extra_body"]["return_progress"] is True
    assert "return_progress" not in calls[1]["extra_body"] and calls[1]["extra_body"]["reasoning_effort"] == "low"
    assert backend._progress_broken is True and failing.closed is True


@pytest.mark.asyncio
async def test_a_failure_after_output_began_is_raised_not_resent():
    failing = _Stream([_Chunk(text="partial")], error=_Boom("invalid OpenAI Chat prompt-progress state"))
    calls, client = _streaming_client(failing)
    stream = await OpenAITransport(client, backend=NS(name="ninfer")).create(purpose="turn", **SKW)
    with pytest.raises(_Boom):
        [chunk async for chunk in stream]
    assert len(calls) == 1, "resending after output would duplicate text"


@pytest.mark.asyncio
async def test_another_stream_error_before_output_is_raised():
    failing = _Stream([], error=_Boom("something else broke"))
    calls, client = _streaming_client(failing)
    backend = NS(name="ninfer")
    stream = await OpenAITransport(client, backend=backend).create(purpose="turn", **SKW)
    with pytest.raises(_Boom):
        [chunk async for chunk in stream]
    assert len(calls) == 1 and not getattr(backend, "_progress_broken", False)
