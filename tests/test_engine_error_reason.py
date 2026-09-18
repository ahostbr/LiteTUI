"""The engine's own reason must reach the user, and a permanent media refusal
must not poison the conversation (T824).

🔴 RYAN RAN `view_image` ON A 35B ARTIFACT STARTED WITHOUT `--vision`.
The engine answered HTTP 400 `vision_disabled`. LiteTUI showed
"Something went wrong talking to the model server." — and then every later
turn failed the same way, because the image part stayed in history and was
re-sent on each request.

Two defects, one report:

1. `_plain_backend_error` only knew `BackendError`. An error the ENGINE raises
   mid-stream arrives as an `openai` exception, so every engine 400 fell
   through to the generic sentence.

       I NAMED THIS GAP IN T806's OWN COMMIT BODY — "errors raised by the
       OpenAI CLIENT during streaming are not BackendErrors and carry their
       body elsewhere, so the table does not reach those" — AND THEN LEFT IT
       OPEN. A DOCUMENTED DEFECT IS NOT A HANDLED DEFECT.

2. A FAILED REQUEST IS AN EVENT; A POISONED HISTORY IS A STATE. Reporting the
   first without clearing the second leaves an accurate message beside a
   thread that can never answer again.

⬜ THE CODES ARE READ FROM THE ENGINE'S CONTRACT, NOT GUESSED:
`ninfer/docs/serving.md:44` names `vision_disabled`, `:331`
`media_budget_exceeded`, and HTTP 413 `request_too_large`. `:49` — "A later
request cannot enable a capability omitted at startup" — is the whole reason
the first one earns a stub and the size ones do not.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from litetui import app as app_mod
from litetui.app import _media_refused_for_good, _plain_backend_error


def _api_error(code: str, status: int = 400, message: str = "refused"):
    """A real `openai` exception, built the way the client builds one.

    Not a SimpleNamespace: the point of these arms is the shape of the
    LIBRARY's exception, and a hand-made stand-in would prove only that my
    idea of it is self-consistent.
    """
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.BadRequestError(
        message, response=response, body={"code": code, "message": message})


# ── the reason survives the client ───────────────────────────────────────────


def test_vision_disabled_says_what_is_wrong_and_what_to_do():
    """🔴 THE SENTENCE RYAN SHOULD HAVE SEEN."""
    said = _plain_backend_error(_api_error("vision_disabled"), "ninfer")
    assert "without vision" in said
    assert "cannot be switched on for one request" in said
    assert "Something went wrong" not in said


def test_the_size_refusals_get_their_own_sentence():
    for code in ("media_budget_exceeded", "request_too_large"):
        said = _plain_backend_error(_api_error(code, status=413), "ninfer")
        assert "too large" in said, (code, said)


def test_a_nested_error_body_is_read_too():
    """⬜ The engine uses both shapes on this wire; `classify_ninfer_error`
    already handles the nesting, which is why the body is handed to IT rather
    than re-parsed here."""
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    response = httpx.Response(400, request=request)
    e = openai.BadRequestError(
        "refused", response=response,
        body={"error": {"code": "vision_disabled", "message": "no vision"}})
    assert "without vision" in _plain_backend_error(e, "ninfer")


def test_an_unknown_code_still_names_the_status():
    """⬜ A status with no code we know beats saying nothing: "400" tells the
    reader it was REFUSED, not dropped on the floor."""
    said = _plain_backend_error(_api_error("some_new_code", status=422), "ninfer")
    assert "422" in said
    assert "refused the request" in said


def test_a_plain_exception_keeps_the_old_sentence():
    """⬜ THE CONTROL. Nothing about this change may widen to errors that carry
    no engine body at all."""
    assert _plain_backend_error(ValueError("boom"), "ninfer") == (
        "Something went wrong talking to the model server.")


# ── the permanent refusal, and only it, clears history ───────────────────────


def test_only_the_capability_refusal_is_permanent():
    """🔴 THE DISCRIMINATOR. Size refusals must NOT trigger a stub: a smaller
    image would have gone through, and stubbing would destroy content the user
    could still use."""
    assert _media_refused_for_good(_api_error("vision_disabled")) is True
    assert _media_refused_for_good(_api_error("media_budget_exceeded")) is False
    assert _media_refused_for_good(_api_error("request_too_large")) is False
    assert _media_refused_for_good(ValueError("boom")) is False


def _born_store(name="probe"):
    """A REAL store on disk, not a recorder double.

    🔴 THE ARM BELOW IS ABOUT REPLAY, so a double that remembers the calls I
    made would prove only that I called it. What has to be true is that
    `ConversationRepository.read` gives back a thread with no image in it, and
    only the real append-only file can answer that.

    ⬜ `paths.CONVO_DIR` is NOT set here: conftest's autouse fixture already
    monkeypatches it to this test's own `tmp_path`, and assigning it again
    would be a second, un-restored redirect of a module global that 48 sites
    read.
    """
    from litetui.conversation import ConversationRepository

    store = ConversationRepository()
    store.stage(name)
    store.convo_dir.mkdir(parents=True, exist_ok=True)
    store.pending = False  # born, the way app.py does it at materialisation
    return store


def _app_with_an_image():
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.store = _born_store()
    a.conversation = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,AA=="}},
            {"type": "text", "text": "[view_image] shot.png"},
        ]},
        {"role": "assistant", "content": "I cannot see it."},
        {"role": "user", "content": "what about now?"},
    ]
    for message in a.conversation:
        a.store.record_msg(message)
    return a


def test_the_image_part_is_stubbed_and_the_question_survives(tmp_path):
    """🔴 THE POISON, CLEARED. The image becomes text; everything else in the
    message — and every other message — is untouched."""
    a = _app_with_an_image()
    assert a._stub_refused_media() == 1

    parts = a.conversation[1]["content"]
    assert [p["type"] for p in parts] == ["text", "text"]
    assert "no vision" in parts[0]["text"]
    # the question that came WITH the image is still there
    assert parts[1]["text"] == "[view_image] shot.png"
    # and nothing else moved
    assert a.conversation[0]["content"] == "you are helpful"
    assert a.conversation[2]["content"] == "I cannot see it."
    assert a.conversation[3]["content"] == "what about now?"


def test_no_image_is_a_no_op_that_reports_zero(tmp_path):
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a.store = _born_store("noop")
    a.conversation = [{"role": "user", "content": "just text"},
                      {"role": "user", "content": [{"type": "text", "text": "parts"}]}]
    before = [dict(m) for m in a.conversation]
    assert a._stub_refused_media() == 0
    assert a.conversation == before


def test_every_image_in_the_thread_goes_not_just_the_newest(tmp_path):
    """⬜ The refusal is about the ENGINE, not about one message. Leaving an
    older image behind would fail the very next turn for the same reason."""
    a = _app_with_an_image()
    a.conversation.append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BB=="}},
        {"type": "text", "text": "and this one"},
    ]})
    assert a._stub_refused_media() == 2
    remaining = [p for m in a.conversation
                 if isinstance(m.get("content"), list)
                 for p in m["content"] if p.get("type") == "image_url"]
    assert remaining == []


def test_the_stub_SURVIVES_A_RELOAD_and_is_not_just_in_memory(tmp_path):
    """🔴 THE HALF THE FIRST VERSION MISSED, AND IT PUT THE BUG BACK.

    `_stub_refused_media` mutated `self.conversation` and nothing else. The
    store is APPEND-ONLY, so the image was still in `convo.jsonl` — and
    `/resume`, or the next launch, replays it straight back into the thread
    that the stub had just cleaned. The next turn 400s again, for the reason
    the stub exists to remove.

        A FIX HELD ONLY IN MEMORY IS A FIX WITH A LIFETIME. The in-memory
        assertions above are all TRUE on the broken build — measured, in
        memory 1 -> 0 and on disk 1 -> 1 — so no arm in this file could see
        it. Only a round trip through the real file can.

    ⬜ THE MESSAGE COUNT IS ASSERTED TOO. `_edit` records ONE message; a
    `record_truncate` or a snapshot would also make the image go away, and
    would take the conversation with it.
    """
    from litetui.conversation import ConversationRepository

    a = _app_with_an_image()
    assert a._stub_refused_media() == 1

    _meta, replayed = ConversationRepository.read(a.store.convo_path)
    a.store.release()

    images = [p for m in replayed if isinstance(m.get("content"), list)
              for p in m["content"] if p.get("type") == "image_url"]
    assert images == [], "the image came back on replay"
    assert len(replayed) == 4, replayed
    assert replayed[1]["content"][1]["text"] == "[view_image] shot.png"
    assert replayed[3]["content"] == "what about now?"


# ── a status code outranks the connection heuristic ──────────────────────────


def test_an_engine_400_whose_prose_collides_is_not_a_dead_server():
    """🔴 FOUND BY MY OWN FIXTURE COLLIDING WITH IT.

    `_connection_family` sniffs the TEXT of non-BackendError exceptions for
    ("winerror 10061", "refused", "timed out", "timeout", "connection error"),
    and it runs BEFORE the body is read. Those are ordinary words in a 400:
    `request_queue_timeout` is in NInfer's own table
    (`ninfer_backend.NINFER_ERROR_ACTION`) and its sentence says the engine
    "did not admit the request in time".

        A HEURISTIC MUST NOT OUTRANK A FACT. `status_code` exists only when a
        response came back; `APIConnectionError` carries none. So a status code
        settles it, and the sniff never sees the message.

    Without the guard the user is told "The model server seems closed — start
    it" about an engine that is up and answering — advice for a situation that
    is not theirs, which is the same failure mode T688 F fixed for llama.cpp.
    """
    for message in ("connection refused by policy", "the request timed out upstream",
                    "media refused"):
        e = _api_error("vision_disabled", message=message)
        said = _plain_backend_error(e, "ninfer")
        assert "seems closed" not in said, (message, said)
        assert "without vision" in said, (message, said)


def test_a_real_connection_failure_is_still_recognised():
    """⬜ THE CONTROL, and the half that must not regress: no status code, so
    the heuristic is still the only thing that can answer."""
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    dead = openai.APIConnectionError(request=request)
    assert getattr(dead, "status_code", None) is None
    assert "seems closed" in _plain_backend_error(dead, "ninfer")
