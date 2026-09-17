"""Pull video IDs + titles for the quant head-to-heads, so transcripts can be fetched."""

import re
import time

from bridge import Chrome

QUERIES = [
    "I Tested Every Qwen3.8-27B Quant best one for your GPU",
    "Qwen 3.8 27B Quantizations Q1 Q8 compared",
    "Qwen3.8 27B Unsloth Dynamic 3.0 Quants Tested",
    "Ridge vs Unsloth vs Bartowski quants Qwen 3.8",
    "Stop Using GGUF for Qwen3.8-27B NVFP4 Advantage",
]

WANT = re.compile(r"qwen\s*3\.?8|27b|quant|unsloth|nvfp4|bartowski", re.I)


def main():
    ch = Chrome().start()
    seen = {}
    try:
        for q in QUERIES:
            url = "https://www.youtube.com/results?search_query=" + q.replace(" ", "+")
            ch.navigate(url)
            time.sleep(3.0)
            html = ch.html()
            # watch links carry their title in the same anchor attributes
            for m in re.finditer(r'"videoId":"([\w-]{11})".{0,400?}', html):
                pass
            for m in re.finditer(
                r'\{"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"(.*?)"\}',
                html,
            ):
                vid, title = m.group(1), m.group(2)
                title = title.encode().decode("unicode_escape", "ignore")
                if vid in seen:
                    continue
                if WANT.search(title):
                    seen[vid] = title
    finally:
        ch.close()

    for vid, title in seen.items():
        print("https://youtu.be/%s  %s" % (vid, title))
    print("\n%d videos" % len(seen))


if __name__ == "__main__":
    main()
