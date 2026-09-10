"""Explicit live OAuth test; sends only synthetic content and executes no tools.

Run: uv run --locked python e2e/oauth_probe.py codex MODEL
Never reads/writes the conversation store or refreshes credentials.
"""

import asyncio
import base64
import json
import sys
from io import BytesIO

from PIL import Image

from litetui.model_transport import OAuthTransport, ProviderError


async def main(provider, model):
    transport = OAuthTransport(provider)
    messages = [
        {
            "role": "system",
            "content": "You are a test assistant inside LiteTUI. Follow the user instructions.",
        },
        {
            "role": "user",
            "content": "Call litetui_echo once with value ORCHID. Do not answer until you receive its result.",
        },
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "litetui_echo",
                "description": "Return the supplied test value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    first = await transport.create(
        model=model, messages=messages, tools=tools, max_tokens=1024, stream=False
    )
    answer = first.choices[0].message
    assert len(answer.tool_calls) == 1, "Expected one tool call"
    call = answer.tool_calls[0]
    assert call.function.name == "litetui_echo"
    assert json.loads(call.function.arguments)["value"] == "ORCHID"
    messages.append(
        {
            "role": "assistant",
            "content": answer.content or None,
            "provider_metadata": answer.provider_metadata,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
            ],
        }
    )
    messages.append(
        {"role": "tool", "tool_call_id": call.id, "content": "ORCHID-VERIFIED"}
    )
    messages.append(
        {
            "role": "user",
            "content": "Reply with the exact tool result, and nothing else.",
        }
    )
    final = await transport.create(
        model=model, messages=messages, tools=tools, max_tokens=1024, stream=False
    )
    assert "ORCHID-VERIFIED" in final.choices[0].message.content, (
        "Missing supplied tool result"
    )
    assert not final.choices[0].message.tool_calls, "Unexpected extra tool call"
    png = BytesIO()
    Image.new("RGB", (128, 128), "red").save(png, format="PNG")
    image_answer = await transport.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "What is the dominant color of this image? Reply with one color word.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(png.getvalue()).decode()
                        },
                    },
                ],
            }
        ],
        max_tokens=1024,
        stream=False,
    )
    assert "red" in image_answer.choices[0].message.content.lower(), (
        "Image was not understood"
    )
    print(
        json.dumps(
            {
                "provider": provider,
                "model": model,
                "tool_roundtrip": "PASS",
                "history_continuation": "PASS",
                "image_input": "PASS",
                "tokens": final.usage.total_tokens,
            }
        )
    )


if __name__ == "__main__":
    try:
        asyncio.run(main(*sys.argv[1:]))
    except (ProviderError, AssertionError) as error:
        print(str(error))
        raise SystemExit(1) from None
