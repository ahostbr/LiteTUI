"""Small explicit live probe. Prints counters only; never logs prompts or credentials."""

import argparse
import asyncio
import json
import time
import uuid

import httpx

from litetui.model_transport import OAuthTransport


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument(
        "--efforts",
        nargs="+",
        default=["low", "low", "medium", "high", "xhigh", "max", "ultra"],
    )
    parser.add_argument("--entries", type=int, default=90)
    args = parser.parse_args()
    assert args.live

    class DiagnosticTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            response = await super().handle_async_request(request)
            if response.status_code == 400:
                await response.aread()
                # Synthetic requests only. Surface the provider's parameter error,
                # never the request headers or authentication store.
                error = response.json().get("error", {})
                print(json.dumps({"status": 400, "error": error}), flush=True)
            return response

    transport = OAuthTransport(
        "codex",
        prompt_cache_key=str(uuid.uuid4()),
        http_transport=DiagnosticTransport(),
    )
    # Above the caching floor, no user/project content and no tools.
    reference = "\n".join(
        f"Reference entry {i}: the audit checks stable conversation history, unchanged tool schemas, "
        "and accurate reporting of cached input tokens. This entry is reference data only."
        for i in range(args.entries)
    )
    messages = [
        {
            "role": "system",
            "content": "Reply with OK only. Do not analyze the reference data.",
        },
        {"role": "user", "content": reference + "\nConfirm receipt with OK."},
    ]
    for effort in args.efforts:
        started = time.monotonic()
        result = await transport.create(
            model="gpt-6-astra",
            messages=messages,
            extra_body={"reasoning_effort": effort},
            tool_choice="none",
        )
        usage = result.usage
        print(
            json.dumps(
                {
                    "effort": effort,
                    "seconds": round(time.monotonic() - started, 2),
                    **{
                        key: getattr(usage, key, None)
                        for key in (
                            "prompt_tokens",
                            "completion_tokens",
                            "cached_tokens",
                            "cache_write_tokens",
                        )
                    },
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
