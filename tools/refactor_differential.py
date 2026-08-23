"""DIFFERENTIAL PROOF: does TurnEngine produce what the inline code produced?

My unit tests were written from the NEW code, so they prove it is
self-consistent, not that it matches b604bc2. This lifts the ORIGINAL inline
blocks out of git by anchor text -- never retyped -- executes them against a
stand-in `self`, and compares the resulting dicts to TurnEngine's over a
matrix of inputs.

A mismatch here means the refactor changed behaviour, whatever the tests say.
"""
import itertools
import subprocess
import sys
import textwrap
import types

sys.path.insert(0, "src")
from litetui.turn_engine import TurnEngine  # noqa: E402
from litetui import llm_backend  # noqa: E402

OLD = subprocess.run(["git", "show", "b604bc2:src/litetui/app.py"],
                     capture_output=True).stdout.decode("utf-8")

# ── lift the two original blocks verbatim ───────────────────────────────
CHAT_START = '            kwargs: dict = {\n                "model": self.model_id or "local-model",\n                # Live store merged in here'
CHAT_END = '            if extra:\n                kwargs["extra_body"] = extra\n'
i = OLD.index(CHAT_START)
j = OLD.index(CHAT_END, i) + len(CHAT_END)
chat_src = textwrap.dedent(OLD[i:j])
assert "kwargs.update(native)" in chat_src

COMPACT_START = '                kwargs: dict = {\n                    "model": self.model_id or "local-model",\n                    "messages": ask,'
COMPACT_END = '                    kwargs["tools"] = self._all_tools()\n'
i = OLD.index(COMPACT_START)
j = OLD.index(COMPACT_END, i) + len(COMPACT_END)
compact_src = textwrap.dedent(OLD[i:j])
assert 'kwargs["tools"]' in compact_src

print(f"lifted from b604bc2: chat block {len(chat_src.splitlines())} lines, "
      f"compact block {len(compact_src.splitlines())} lines")


def run_old(src, self_obj, extra_names=None):
    g = {"llm_backend": llm_backend, "self": self_obj}
    g.update(extra_names or {})
    exec(compile(src, "<b604bc2>", "exec"), g)
    return g["kwargs"]


class FakeSelf:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def _all_tools(self):
        return self._tools_value


# ── matrix ──────────────────────────────────────────────────────────────
TOOLS = [{"type": "function", "function": {"name": "bash"}}]
mismatches = 0
checked = 0

for tools_enabled, thinking, overrides in itertools.product(
    [False, True],
    [None, "off", "low", "high", "xhigh"],
    [{}, {"temperature": 0.7}, {"top_k": 40}, {"temperature": 0.2, "top_k": 5, "min_p": 0.1}],
):
    msgs = [{"role": "user", "content": "hi"}]
    fake = FakeSelf(
        model_id="qwen",
        tools_enabled=tools_enabled,
        thinking_level=thinking,
        settings=types.SimpleNamespace(max_tokens_tools=111, max_tokens_chat=222),
        backend=types.SimpleNamespace(request_overrides=lambda m, o=overrides: o),
        _tools_value=TOOLS,
    )
    old = run_old(chat_src, fake, {"request_messages": msgs})
    new = TurnEngine.chat_request(
        model_id="qwen", messages=msgs, tools_enabled=tools_enabled,
        max_tokens_tools=111, max_tokens_chat=222,
        request_overrides=overrides, thinking_level=thinking,
        tools=TOOLS if tools_enabled else None,
    )
    checked += 1
    if old != new:
        mismatches += 1
        print(f"\nMISMATCH chat: tools={tools_enabled} thinking={thinking} ov={overrides}")
        print("  old:", old)
        print("  new:", new)

for tools_enabled, level in itertools.product([False, True],
                                              ["off", "low", "medium", "xhigh"]):
    ask = [{"role": "user", "content": "summarise"}]
    fake = FakeSelf(
        model_id="qwen",
        tools_enabled=tools_enabled,
        settings=types.SimpleNamespace(compact_max_tokens=333,
                                       compact_thinking_level=level),
        _tools_value=TOOLS,
    )
    old = run_old(compact_src, fake, {"ask": ask})
    new = TurnEngine.compact_request(
        model_id="qwen", messages=ask, max_tokens=333, thinking_level=level,
        tools_enabled=tools_enabled, tools=TOOLS if tools_enabled else None,
    )
    checked += 1
    if old != new:
        mismatches += 1
        print(f"\nMISMATCH compact: tools={tools_enabled} level={level}")
        print("  old:", old)
        print("  new:", new)

# ── the None-model fallback, both builders ──────────────────────────────
for mid in (None, ""):
    fake = FakeSelf(model_id=mid, tools_enabled=False, thinking_level=None,
                    settings=types.SimpleNamespace(max_tokens_tools=1, max_tokens_chat=2),
                    backend=types.SimpleNamespace(request_overrides=lambda m: {}),
                    _tools_value=[])
    old = run_old(chat_src, fake, {"request_messages": []})
    new = TurnEngine.chat_request(model_id=mid, messages=[], tools_enabled=False,
                                  max_tokens_tools=1, max_tokens_chat=2,
                                  request_overrides={}, thinking_level=None, tools=None)
    checked += 1
    if old != new:
        mismatches += 1
        print(f"\nMISMATCH chat model_id={mid!r}: {old} != {new}")

print(f"\n{checked} input combinations compared against b604bc2")
print("IDENTICAL — behaviour preserved" if not mismatches
      else f"{mismatches} MISMATCHES — behaviour CHANGED")
sys.exit(1 if mismatches else 0)
