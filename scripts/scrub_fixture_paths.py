"""Replace real machine roots in recorded test fixtures with synthetic ones.

The fixtures were captured from real runs, so they carry the capture machine's worktree and
temp roots. Public repo rule (Ryan): directories never go public. Each root is replaced with a
synthetic root of the same separator style, byte for byte everywhere else:

    <drive>:<sep>Projects<sep>.worktrees<sep>OpenBolt-rust-tui  ->  C:<sep>fixture<sep>worktree
    <drive>:<sep>Users<sep>Ryan<sep>AppData<sep>Local<sep>Temp  ->  C:<sep>fixture<sep>temp

    python scripts/scrub_fixture_paths.py FILE...
"""
import re
import sys
from pathlib import Path

RULES = [
    (re.compile(r"C:(?P<s>[\\/]+)Projects(?P=s)\.worktrees(?P=s)OpenBolt-rust-tui"), r"C:\g<s>fixture\g<s>worktree"),
    (re.compile(r"C:(?P<s>[\\/]+)Users(?P=s)Ryan(?P=s)AppData(?P=s)Local(?P=s)Temp"), r"C:\g<s>fixture\g<s>temp"),
]

for name in sys.argv[1:]:
    path = Path(name)
    text = path.read_bytes().decode("utf-8")
    total = 0
    for pattern, replacement in RULES:
        text, count = pattern.subn(replacement, text)
        total += count
    path.write_bytes(text.encode("utf-8"))
    print(f"{total:4d}  {name}")
