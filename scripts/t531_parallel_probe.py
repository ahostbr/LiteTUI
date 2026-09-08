"""T531: what LM Studio's parallel slots do to per-request and aggregate speed.

Fires N identical streaming chat requests at once against :1234 and reports,
per request: time to first token, wall time, completion tokens, tok/s; then the
aggregate tok/s for the batch. Run with N=1, 2, 4 (the loaded model's slot
count) and compare. Usage: python t531_parallel_probe.py [N] [model]
"""
from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1
MODEL = sys.argv[2] if len(sys.argv) > 2 else "qwen3.5-4b-claude-4.6-opus-reasoning-distilled"
PROMPT = "Write a 300-word plain-prose description of how a four-stroke engine works. No lists."
URL = "http://127.0.0.1:1234/v1/chat/completions"

results: list[dict] = [None] * N  # type: ignore


def one(i: int, go: threading.Event) -> None:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": int(__import__("os").environ.get("T531_MAX_TOKENS", "400")),
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={"content-type": "application/json"})
    go.wait()
    t0 = time.perf_counter()
    first = None
    toks = 0
    usage = None
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    ev = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if ev.get("usage"):
                    usage = ev["usage"]
                ch = ev.get("choices") or []
                if not ch:
                    continue
                delta = ch[0].get("delta") or {}
                if delta.get("content") or delta.get("reasoning_content"):
                    if first is None:
                        first = time.perf_counter() - t0
                    toks += 1
    except Exception as e:  # noqa: BLE001
        results[i] = {"error": f"{type(e).__name__}: {e}", "wall": time.perf_counter() - t0}
        return
    wall = time.perf_counter() - t0
    ctoks = (usage or {}).get("completion_tokens") or toks
    results[i] = {"ttft": first, "wall": wall, "completion_tokens": ctoks, "tok_s": ctoks / wall if wall else 0}


go = threading.Event()
threads = [threading.Thread(target=one, args=(i, go), daemon=True) for i in range(N)]
for t in threads:
    t.start()
t_batch = time.perf_counter()
go.set()
for t in threads:
    t.join()
batch_wall = time.perf_counter() - t_batch

print(f"N={N} model={MODEL}")
total = 0
for i, r in enumerate(results):
    if "error" in r:
        print(f"  req{i}: ERROR {r['error']} after {r['wall']:.1f}s")
        continue
    total += r["completion_tokens"]
    print(f"  req{i}: ttft={r['ttft']:.2f}s wall={r['wall']:.1f}s tokens={r['completion_tokens']} per-request={r['tok_s']:.1f} tok/s")
print(f"  batch: wall={batch_wall:.1f}s aggregate={total / batch_wall:.1f} tok/s")
