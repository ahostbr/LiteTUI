"""Replace real machine paths in docs with placeholders (public repo: directories never go public).

    python scripts/scrub_doc_paths.py FILE...
"""
import re
import sys
from pathlib import Path

S = r"[\\/]+"
RULES = [
    (re.compile(r"C:" + S + r"Projects" + S + r"\.worktrees" + S + r"OpenBolt-rust-tui"), "<worktree>"),
    (re.compile(r"C:" + S + r"Projects" + S + r"LiteTUI" + S + r"\.venv" + S + r"Scripts" + S + r"python\.exe"),
     "<LiteTUI venv python>"),
    (re.compile(r"C:" + S + r"Projects" + S + r"LiteSound"), "<LiteSound repo>"),
    (re.compile(r"C:" + S + r"Projects" + S + r"LiteTUI"), "<LiteTUI repo>"),
    (re.compile(r"C:" + S + r"Projects" + S + r"LiteSuite"), "<LiteSuite repo>"),
    (re.compile(r"C:" + S + r"Projects" + S + r"\.scratch"), "<scratch>"),
    (re.compile(r"C:" + S + r"Projects" + S + r"your-project"), "<your-project>"),
    (re.compile(r"C:" + S + r"Users" + S + r"Ryan" + S + r"AppData" + S + r"Local" + S + r"Temp"), "<temp>"),
    (re.compile(r"C:" + S + r"Users" + S + r"Ryan"), "<home>"),
    (re.compile(r"C:" + S + r"Projects"), "<projects>"),
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
