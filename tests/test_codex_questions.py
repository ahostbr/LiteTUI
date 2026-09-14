import asyncio
from types import SimpleNamespace as NS

import pytest

from litetui.ask_user_question import _parse_questions, _serialize
from litetui.codex_app_server import AppServerTransport
from litetui.question_result import capture_answers, native_answers


@pytest.mark.asyncio
async def test_question_origin_is_task_local_and_does_not_leak_to_other_backends():
    from litetui.question_result import question_origin, question_origin_fields

    async def read_origin(ident):
        with question_origin("thread", ident, "item", delivery="async"):
            await asyncio.sleep(0)
            result = await asyncio.to_thread(question_origin_fields)
            result["provider"] = "mutated-copy"
            assert question_origin_fields()["provider"] == "codex"
            return result["turnId"]

    assert await asyncio.gather(read_origin("one"), read_origin("two")) == ["one", "two"]
    assert question_origin_fields() == {}


def submitted(action="submit"):
    return {
        "action": action,
        "questions": [
            {
                "label": "Choose",
                "options": [{"title": "One"}, {"title": "Two"}],
                "selected": [1],
                "note": "Extra detail",
                "answered": True,
            }
        ],
    }


@pytest.mark.asyncio
async def test_capture_crosses_tool_thread_without_changing_text_contract():
    with capture_answers() as captured:
        text = await asyncio.to_thread(_serialize, submitted())
    assert "SUBMITTED" in text
    assert native_answers(["q"], captured[0]) == {
        "q": {"answers": ["Two", "Extra detail"]}
    }
    with capture_answers() as other:
        assert not other


@pytest.mark.parametrize("action", ["chat", "cancelled"])
def test_partial_or_cancelled_is_not_an_answer(action):
    assert native_answers(["q"], submitted(action)) == {}


def test_native_free_text_and_secret_questions():
    states = _parse_questions(
        {
            "questions": [
                {
                    "question": "Enter value",
                    "options": [],
                    "allowFreeText": True,
                    "isSecret": True,
                }
            ]
        }
    )
    assert states[0].secret and states[0].options == []
    assert states[0].to_dict()["isSecret"] is True


@pytest.mark.asyncio
async def test_native_request_uses_shared_tool_door_and_structured_ids():
    replies = []
    calls = []

    async def send(message):
        replies.append(message)

    async def execute(name, args):
        calls.append(name)
        return await asyncio.to_thread(_serialize, submitted()), True

    app = NS(_execute_tool=execute)
    transport = AppServerTransport(NS(send=send), app)
    await transport._server_request(
        {
            "id": 1,
            "method": "item/tool/requestUserInput",
            "params": {
                "questions": [
                    {
                        "id": "native-id",
                        "question": "Choose",
                        "options": [{"label": "One"}, {"label": "Two"}],
                    }
                ]
            },
        }
    )
    assert calls == ["ask_user_question"]
    assert replies[-1]["result"] == {
        "answers": {"native-id": {"answers": ["Two", "Extra detail"]}}
    }
