"""YouTube search for Qwen3.8-27B quant head-to-heads, via the Chrome bridge.

Ryan 2026-09-15: four Qwen3.8-27B quants are on disk (lmstudio-community Q4_K_M,
unsloth UD-Q4_K_M, Zynerji Ektome uncensored, esatapedico NVFP4-MTP) and he wants
to know which wins right now.

Reads the results page rather than the API: no key, and it sees what he'd see.
"""

import re
import sys
import time

from bridge import Chrome

QUERIES = [
    "Qwen3.8 27B quant comparison Q4_K_M vs UD",
    "unsloth dynamic quant vs standard Q4_K_M benchmark",
    "Qwen3.8 27B NVFP4 vs Q4_K_M",
    "Qwen3.8 27B local benchmark test",
]


def scrape(ch, q):
    url = "https://www.youtube.com/results?search_query=" + q.replace(" ", "+") + "&sp=CAI%253D"
    ch.navigate(url)
    time.sleep(3.5)
    txt = ch.text()
    return txt


def main():
    ch = Chrome().start()
    try:
        for q in QUERIES:
            print("\n" + "=" * 78)
            print("QUERY:", q)
            print("=" * 78)
            try:
                txt = scrape(ch, q)
            except Exception as e:
                print("  FAILED:", e)
                continue
            # Titles on the results page sit on their own lines followed by a
            # channel + view-count line; keep anything that names the model.
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            hits = []
            for i, l in enumerate(lines):
                if re.search(r"qwen\s*3\.?8|qwen3\.8|27b|quant|gguf|unsloth|nvfp4", l, re.I):
                    if 15 < len(l) < 160:
                        ctx = " | ".join(lines[i : i + 3])[:300]
                        hits.append(ctx)
            seen = set()
            out = []
            for h in hits:
                k = h[:60]
                if k in seen:
                    continue
                seen.add(k)
                out.append(h)
            for h in out[:12]:
                print("  -", h)
            if not out:
                print("  (no matching titles on the results page)")
    finally:
        ch.close()


if __name__ == "__main__":
    sys.exit(main())
