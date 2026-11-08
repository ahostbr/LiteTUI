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
