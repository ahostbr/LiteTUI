"""Tests for the .convos persistence layer, /compact tail safety, and helpers.

Pure-function level: no TUI, no LM Studio, no network. Run: python test_convos.py

These exist because a persistence bug is silent. You do not find out that a
conversation was saved wrong until you /resume it, which is the one moment you
cannot afford to.
"""

import json
import tempfile
from pathlib import Path

from app import LMStudioChat as A

results = []


def check(label, fn):
    try:
        fn()
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}")
        return results.append(False)
    except Exception as e:
        print(f"  FAIL  {label}: {type(e).__name__}: {e}")
        return results.append(False)
    print(f"  ok    {label}")
    results.append(True)


def eq(got, want):
    assert got == want, f"expected {want!r}, got {got!r}"


def write(path: Path, records: list[dict], tail_garbage: str = "") -> Path:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        if tail_garbage:
            f.write(tail_garbage)
    return path


tmp = Path(tempfile.mkdtemp(prefix="convos-test-"))

# ── replay semantics ────────────────────────────────────────────
def t_msgs_replay():
    p = write(tmp / "a.jsonl", [
        {"type": "meta", "id": "A", "created": 1},
        {"type": "msg", "message": {"role": "system", "content": "sys"}},
        {"type": "msg", "message": {"role": "user", "content": "hello"}},
        {"type": "msg", "message": {"role": "assistant", "content": "hi"}},
    ])
    meta, msgs = A._read_convo(p)
    eq(meta["id"], "A")
    eq([m["role"] for m in msgs], ["system", "user", "assistant"])


def t_snapshot_replaces():
    """The whole point of the snapshot type: it must WIN over earlier msgs."""
    p = write(tmp / "b.jsonl", [
        {"type": "meta", "id": "B"},
        {"type": "msg", "message": {"role": "user", "content": "old-one"}},
        {"type": "msg", "message": {"role": "user", "content": "old-two"}},
        {"type": "snapshot", "reason": "compact",
         "messages": [{"role": "system", "content": "sys"},
                      {"role": "user", "content": "SUMMARY"}]},
        {"type": "msg", "message": {"role": "assistant", "content": "after"}},
    ])
    _, msgs = A._read_convo(p)
    eq([A._flatten(m["content"]) for m in msgs], ["sys", "SUMMARY", "after"])
    # negative control: the pre-snapshot text must NOT survive
    assert "old-one" not in json.dumps(msgs), "pre-snapshot message resurrected"


def t_torn_line_tolerated():
    """A hard kill mid-write leaves a partial final line. It must not lose the rest."""
    p = write(tmp / "c.jsonl", [
        {"type": "meta", "id": "C"},
        {"type": "msg", "message": {"role": "user", "content": "kept"}},
    ], tail_garbage='{"type": "msg", "mess')
    _, msgs = A._read_convo(p)
    eq(len(msgs), 1)
    eq(msgs[0]["content"], "kept")


# ── /compact tail safety ────────────────────────────────────────
def t_tail_starts_on_user():
    msgs = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "res"},
        {"role": "assistant", "content": "a1"},
    ]
    # asking for 3 would start on the tool-calling assistant -> must trim forward
    eq(A._safe_tail(msgs, 3), [])
    eq(A._safe_tail(msgs, 4), msgs)


def t_tail_no_orphan_tool():
    """Whatever the window, the tail must never BEGIN with a tool message."""
    msgs = [
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "x"}]},
        {"role": "tool", "tool_call_id": "x", "content": "r"},
    ]
    for want in range(0, 5):
        tail = A._safe_tail(msgs, want)
        assert not tail or tail[0]["role"] == "user", f"want={want} tail starts {tail[0]['role']}"


# ── helpers ─────────────────────────────────────────────────────
def t_flatten_image_turn():
    content = [
        {"type": "image_url", "image_url": {"url": "data:..."}},
        {"type": "text", "text": "Describe this image."},
    ]
    eq(A._flatten(content), "[image] Describe this image.")
    eq(A._flatten("plain"), "plain")
    eq(A._flatten(None), "")


def t_title_skips_non_user():
    eq(A._convo_title([{"role": "system", "content": "sys"},
                       {"role": "user", "content": "the real title"}]), "the real title")
    eq(A._convo_title([{"role": "assistant", "content": "x"}]), "(no user message)")
    long = "y" * 100
    assert A._convo_title([{"role": "user", "content": long}]).endswith("…")


def t_chars_counts_image_text():
    msgs = [{"role": "user", "content": "abc"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": [{"type": "text", "text": "de"}]}]
    eq(A._msg_chars(msgs), 5)


for name, fn in list(globals().items()):
    if name.startswith("t_"):
        check(name[2:].replace("_", " "), fn)

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
