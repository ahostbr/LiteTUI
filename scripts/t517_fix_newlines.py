"""T517: the head f-strings in tasks.start_text got REAL newlines where the escape was meant."""
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "src" / "litetui" / "tasks.py"
s = p.read_text(encoding="utf-8")
broken1 = '{task.label}]\n" if not promoted_after else'
fixed1 = '{task.label}]\\n" if not promoted_after else'
broken2 = 'its own timeout still applies]\n"\n    )'
fixed2 = 'its own timeout still applies]\\n"\n    )'
assert broken1 in s and broken2 in s, "shape changed"
s = s.replace(broken1, fixed1).replace(broken2, fixed2)
p.write_text(s, encoding="utf-8", newline="\n")
compile(s, str(p), "exec")
print("tasks.py compiles")
